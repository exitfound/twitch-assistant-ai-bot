"""What the bot sees when someone talks to it in free text, and the fallback ladder.

The bot gets the gist of the chat, not the chat itself: the pulse (src/gemini/pulse.py –
what the chat is about right now, retold), the last CONTEXT_CHAT_MESSAGES as the thread
it answers in, the dossiers of the asker and of the people named, and the previous
stream's chronicle. From raw lines the model pastes pieces back in quotes.

Gemini's input filter cannot be switched off and judges the whole request by
combinations of messages. A blocked request is asked again with less, one rung at a time:
  1. everything
  2. without search and «language»
  3. the thread and the question alone
A block comes back in ~0.3 s, so a rung costs almost no time. The output filter is
random rather than about the size, so an answer it stopped is asked once more on the
same rung; an answer with nothing in it goes straight to the last rung, and no answer
at all (timeout, network) ends the walk.

The context probe (src/cli/probe.py) uses this module too, so what is measured is what is sent.
"""
import asyncio
import logging
import re
from dataclasses import dataclass

from google.genai import types

from src.core.config import Context
from src.core.content import Content
from src.core.database import get_random_knowledge, get_recent_chat, get_relevant_facts, search_context
from src.core.utils import clean_nick
from src.gemini import dossier
from src.gemini.context import ContextBuilder
from src.gemini.ladder import Rung, unique_rungs, walk
from src.gemini.memory import storage

logger = logging.getLogger(__name__)

# Dossiers in one answer: the asker plus up to three people named in the question
MAX_PEOPLE = 4
# A Twitch login: 4–25 Latin letters, digits and underscores, standing as a whole word.
# Russian words are never logins, so they are not looked up
_NICK = re.compile(r'(?<!\w)[a-z0-9_]{4,25}(?!\w)', re.IGNORECASE)


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
    # What the chat is about right now (src/gemini/pulse.py); None – not built yet
    pulse: str | None = None


def is_stream_session(session_id: str) -> bool:
    # 'YYYY-MM-DD HH:MM' – a stream, 'YYYY-MM-DD' – a day without one
    return len(session_id) > 10


async def _people(user: str, prompt: str) -> list[str]:
    """Dossiers of the asker and of the chatters named in the question. The cap counts
    dossiers, not words: «dota» and «pubg» are looked up and take no place."""
    named = [clean_nick(w) for w in _NICK.findall(prompt)]
    return await dossier.people([user, *named], prompt, Context.DOSSIER_FACTS, Context.DOSSIER_FACTS // 2,
                                limit=MAX_PEOPLE)


async def ladder(q: Question) -> list[Rung]:
    """(rung name, prompt), richest first; each next one is what to send when the
    previous was blocked. Identical rungs are dropped."""
    stream = q.stream if q.stream is not None else is_stream_session(q.session_id)
    thread, facts, found, language, people, chronicle = await asyncio.gather(
        get_recent_chat(q.session_id, Context.CHAT_MESSAGES, q.before_id),
        get_relevant_facts(q.user, q.prompt),
        search_context(q.prompt, Context.SEARCH_RESULTS),
        get_random_knowledge(Context.KNOWLEDGE_RANDOM),
        _people(q.user, q.prompt),
        # Offline the previous stream is simply the latest one
        storage.chronicle_before(q.session_id if stream else None),
    )
    question = Content.prompt('user_question', user=q.user, prompt=q.prompt)

    def build(*, memory: bool = True, extras: bool = True) -> str:
        b = ContextBuilder()
        if memory:
            b.add_lines(Content.label('chronicle'), [chronicle] if chronicle else [])
            b.add_lines(Content.label('pulse'), [q.pulse] if q.pulse else [])
        b.add_pairs(Content.label('chat'), thread)
        if memory:
            b.add_pairs(Content.label('facts'), facts)
            b.add_lines(Content.label('dossier'), people)
        if extras:
            b.add_lines(Content.label('channel'), found)
            b.add_lines(Content.label('language'), language)
        b.add_lines(Content.label('replied'), [q.replied] if q.replied else [])
        if memory and (people or chronicle or q.pulse):
            b.add_raw(Content.prompt('memory_hint'))
        return b.add_raw(question).build()

    return unique_rungs([
        ('всё', build()),
        ('без поиска и языка', build(extras=False)),
        ('только чат', build(memory=False, extras=False)),
    ])


async def answer(q: Question, config: types.GenerateContentConfig) -> tuple[str | None, str]:
    """The answer and the rung that gave it."""
    return await walk(await ladder(q), config, q.user)
