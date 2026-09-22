"""!who and !versus: what the bot knows about a chatter, sampled afresh on every call.

!who is not a dossier: it fires off a few things about the person, and every call
digs up something else. !versus piles up such facts about two people, mocks them and
names the loser – whoever's facts are dumber and more out of place. Each call therefore
draws a fresh random sample from the whole history; a fixed window of the last messages
covers about 1% of an active chatter and only their latest topic:
  - events from the memory (chatter_events): ready-made facts, all time
  - messages spread over all their streams, only ones with some content
  - a couple of relations from the profile
  - the last few messages, for whatever they are up to right now
  - what the bot already said about them, so it does not repeat itself
The profile's portrait is left out on purpose: it is a finished summary, and the
model retells it the same way every time. !versus takes half the sample per
person, so two people fit one request.

A blocked request is asked again with less, as free-text answers do
(ladder.walk()): the whole sample → a smaller one → the old context.

The per-stream limits live with the handlers (_per_stream() in commands.py).
"""
import asyncio
import random
from dataclasses import dataclass

from src.core.config import Context
from src.core.content import Content
from src.core.database import (
    get_tagged_answers, get_user_interactions, get_user_messages,
)
from src.gemini.context import ContextBuilder
from src.gemini.ladder import Rung, unique_rungs
from src.gemini.memory import storage

# Shorter messages («ахах», «+», «го») say nothing about a person
MIN_CHARS = 25
# The bot's previous answers about the same target or pair, sent as «already said»
PAST_ANSWERS = 2

# The kinds answers are recorded under in bot_uses: the per-stream limits count them
WHO_KIND = 'who'
VERSUS_KIND = 'versus'


def who_tag(target: str) -> str:
    """How a !who answer is saved in bot_interactions."""
    return f'[who] {target}'


def versus_tag(nick1: str, nick2: str) -> str:
    return f'[versus] {nick1} vs {nick2}'


def interaction_lines(username: str, interactions: list[tuple[str, str]]) -> list[str]:
    return [
        Content.prompt('interaction_line', user=username, question=q, answer=a)
        for q, a in interactions
    ]


@dataclass
class Material:
    """One chatter's sample for one call."""
    nick: str
    facts: list[tuple[str, str]]
    events: list[str]
    sample: list[str]
    messages: list[str]         # the last ones, oldest first: the old context
    recent: list[str]           # the tail of messages that goes into the sample
    relations: list[str]
    dialogue: list[str]         # their past questions to the bot

    @property
    def known(self) -> bool:
        return bool(self.facts or self.events or self.messages or self.dialogue)

    def add_sample(self, b: ContextBuilder, share: float) -> ContextBuilder:
        """The random sample; share < 1 cuts events and messages for a smaller rung."""
        t = self.nick
        return (b.add_pairs(Content.label('user_facts', target=t), self.facts)
                .add_lines(Content.label('user_events', target=t), self.events[:round(len(self.events) * share)])
                .add_lines(Content.label('user_sample', target=t), self.sample[:round(len(self.sample) * share)])
                .add_lines(Content.label('user_relations', target=t), self.relations)
                .add_lines(Content.label('user_recent', target=t), self.recent)
                .add_lines(Content.label('user_interactions', target=t), self.dialogue))

    def add_old(self, b: ContextBuilder) -> ContextBuilder:
        """The context the commands had before the sample: the last messages."""
        t = self.nick
        return (b.add_pairs(Content.label('user_facts', target=t), self.facts)
                .add_lines(Content.label('user_messages', target=t), self.messages)
                .add_lines(Content.label('user_interactions', target=t), self.dialogue))


async def material(nick: str, part: float, messages_n: int) -> Material:
    """A fresh sample of the chatter; part – the share of !who's sample sizes."""
    facts, events, sample, messages, interactions, profile = await asyncio.gather(
        storage.facts_naming(nick),
        storage.random_events(nick, round(Context.WHO_EVENTS * part)),
        storage.random_messages(nick, round(Context.WHO_SAMPLE_MESSAGES * part), MIN_CHARS),
        get_user_messages(nick, messages_n),
        get_user_interactions(nick, Context.USER_INTERACTIONS),
        storage.get_profile(nick),
    )
    relations = profile.relations if profile else []
    relations = [f'{r["nick"]} – {r["note"]}'
                 for r in random.sample(relations, min(Context.WHO_RELATIONS, len(relations)))]
    recent_n = round(Context.WHO_RECENT_MESSAGES * part)
    return Material(nick, facts, events, sample, messages,
                    messages[-recent_n:] if recent_n else [], relations,
                    interaction_lines(nick, interactions))


async def who_rungs(user: str, target: str) -> list[Rung] | None:
    """(rung name, prompt), richest first; None when nothing is known about the target."""
    m, said = await asyncio.gather(
        material(target, 1, Context.WHO_MESSAGES),
        get_tagged_answers([who_tag(target)], PAST_ANSWERS),
    )
    if not m.known:
        return None
    question = Content.prompt('who', user=user, target=target)

    def build(share: float) -> str:
        return (m.add_sample(ContextBuilder(), share)
                .add_lines(Content.label('who_said', target=target), said)
                .add_raw(question).build())

    return unique_rungs([
        ('вся выборка', build(1)),
        ('выборка поменьше', build(0.4)),
        (f'последние {Context.WHO_MESSAGES}', m.add_old(ContextBuilder()).add_raw(question).build()),
    ])


async def versus_material(nick1: str, nick2: str) -> tuple[Material, Material]:
    """Half of !who's sample per person, so the two fit one request."""
    return await asyncio.gather(
        material(nick1, 0.5, Context.VERSUS_MESSAGES),
        material(nick2, 0.5, Context.VERSUS_MESSAGES),
    )


async def versus_rungs(user: str, m1: Material, m2: Material) -> list[Rung]:
    said = await get_tagged_answers(
        [versus_tag(m1.nick, m2.nick), versus_tag(m2.nick, m1.nick)], PAST_ANSWERS,
    )
    question = Content.prompt('versus', user=user, nick1=m1.nick, nick2=m2.nick)
    pair = f'{m1.nick} и {m2.nick}'

    def build(share: float) -> str:
        b = m2.add_sample(m1.add_sample(ContextBuilder(), share), share)
        return b.add_lines(Content.label('who_said', target=pair), said).add_raw(question).build()

    return unique_rungs([
        ('вся выборка', build(1)),
        ('выборка поменьше', build(0.4)),
        (f'последние {Context.VERSUS_MESSAGES}',
         m2.add_old(m1.add_old(ContextBuilder())).add_raw(question).build()),
    ])
