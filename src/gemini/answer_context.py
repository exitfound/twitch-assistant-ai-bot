"""What the bot sees when someone talks to it in free text, and the fallback ladder.

During a stream the bot gets the whole current stream and the whole previous one,
plus its memory – the profiles of the asker and of the people named, and the previous
stream's chronicle. Offline (the session is a date) the chat is the day's last
CONTEXT_CHAT_MESSAGES.

Gemini's input filter cannot be switched off and judges the whole request by
combinations of messages, blocking roughly every fifth request that carries two
streams. A blocked request is asked again with less, one rung at a time:
  1. current stream + previous stream
  2. the whole current stream
  3. the last CONTEXT_CHAT_MESSAGES of it, still with memory
  4. the same without memory, search and «language»
A block comes back in ~0.3 s, so a rung costs almost no time. The output filter is
random rather than about the size, so an answer it stopped is asked once more on the
same rung; an answer with nothing in it goes straight to the last rung, and no answer
at all (timeout, network) ends the walk.

The ladder only decides how much of the stored data goes into one request. The context
probe (src/cli/probe.py) uses this module too, so what is measured is what is sent.
"""
import asyncio
import logging
import re
from dataclasses import dataclass

from google.genai import types

from src.core.config import Context, Memory
from src.core.content import Content
from src.core.database import (
    get_previous_chat_session, get_random_knowledge, get_recent_chat, get_relevant_facts,
    search_context,
)
from src.core.utils import clean_nick
from src.gemini.context import ContextBuilder
from src.gemini.ladder import Rung, unique_rungs, walk
from src.gemini.memory import storage

logger = logging.getLogger(__name__)

# Profiles in one answer: the asker plus up to three people named in the question
MAX_PEOPLE = 4
_NICK = re.compile(r'@?[\w]{3,}')


@dataclass
class Question:
    session_id: str
    user: str
    prompt: str
    # The bot's line this message replies to, if it is a reply to the bot
    replied: str | None = None
    # Only chat before this message id – the probe asks past questions
    before_id: int | None = None
    # Treat the session as a stream whatever its id says: a date-shaped id may still
    # cover the chat of one stream (probe only)
    stream: bool | None = None


def is_stream_session(session_id: str) -> bool:
    # 'YYYY-MM-DD HH:MM' – a stream, 'YYYY-MM-DD' – a day without one
    return len(session_id) > 10


async def _people(user: str, prompt: str) -> list[str]:
    """Profiles of the asker and of the chatters named in the question."""
    named = [clean_nick(w) for w in _NICK.findall(prompt)]
    lines = []
    for nick in dict.fromkeys([user, *named]):
        if len(lines) >= MAX_PEOPLE:
            break
        profile = await storage.get_profile(nick)
        if profile is None:
            continue
        relations = '; '.join(f'{r["nick"]} – {r["note"]}' for r in profile.relations)
        lines.append(f'{nick}: {profile.portrait}' + (f' Отношения: {relations}' if relations else ''))
    return lines


async def ladder(q: Question) -> list[Rung]:
    """(rung name, prompt), richest first; each next one is what to send when the
    previous was blocked. Identical rungs (a short stream, no previous one) are dropped."""
    stream = q.stream if q.stream is not None else is_stream_session(q.session_id)
    chat_limit = Context.STREAM_MAX_MESSAGES if stream else Context.CHAT_MESSAGES
    previous_session = (
        await get_previous_chat_session(q.session_id, Memory.CONVERSATION_MIN_MESSAGES)
        if stream else None
    )
    current, previous, facts, found, language, people, chronicle = await asyncio.gather(
        get_recent_chat(q.session_id, chat_limit, q.before_id),
        get_recent_chat(previous_session, Context.STREAM_MAX_MESSAGES) if previous_session else _nothing(),
        get_relevant_facts(q.user, q.prompt),
        search_context(q.prompt, Context.SEARCH_RESULTS),
        get_random_knowledge(Context.KNOWLEDGE_RANDOM),
        _people(q.user, q.prompt),
        # Offline the previous stream is simply the latest one
        storage.chronicle_before(q.session_id if stream else None),
    )
    recent = current[-Context.CHAT_MESSAGES:]
    question = Content.prompt('user_question', user=q.user, prompt=q.prompt)

    def build(chat: list, prev: list, *, memory: bool = True, extras: bool = True) -> str:
        b = ContextBuilder().add_pairs(Content.label('facts'), facts)
        if memory:
            b.add_lines(Content.label('people'), people)
            b.add_lines(Content.label('chronicle'), [chronicle] if chronicle else [])
        b.add_pairs(Content.label('prev_stream'), prev)
        b.add_pairs(Content.label('chat'), chat)
        if extras:
            b.add_lines(Content.label('channel'), found)
            b.add_lines(Content.label('language'), language)
        b.add_lines(Content.label('replied'), [q.replied] if q.replied else [])
        if memory and (people or chronicle):
            b.add_raw(Content.prompt('memory_hint'))
        return b.add_raw(question).build()

    rungs = []
    if stream:
        rungs += [('два стрима', build(current, previous)), ('весь стрим', build(current, []))]
    rungs += [
        (f'последние {Context.CHAT_MESSAGES}', build(recent, [])),
        ('без памяти и поиска', build(recent, [], memory=False, extras=False)),
    ]
    return unique_rungs(rungs)


async def _nothing() -> list:
    return []


async def answer(q: Question, config: types.GenerateContentConfig) -> tuple[str | None, str]:
    """The answer and the rung that gave it."""
    return await walk(await ladder(q), config, q.user)
