"""Dossier facts that rest: a fact the bot keeps hammering leaves the context for a while.

A fact rests once it went into CONTEXT_FACT_SHOWS answers that reached chat, or the bot
used it in CONTEXT_FACT_USES of them, within the last CONTEXT_FACT_REST_MINUTES. Use is
judged by a background request after the answer reached chat, outside the persona's
prompt: the bot never sees its own lines. Kept in memory, a restart rests nothing.
"""
import asyncio
import logging
import re
import time
from collections import deque

from src.core.config import Context
from src.core.content import Content
from src.gemini.client import background, generate, make_gen_config
from src.gemini.memory.storage import Fact

logger = logging.getLogger(__name__)

# A check, not the persona: the same answer for the same input
TEMPERATURE = 0.0

# fact id → monotonic times it went into an answer / was used in one
_shown: dict[int, deque[float]] = {}
_used: dict[int, deque[float]] = {}
# Checks still running: asyncio keeps only a weak reference to a task
_checks: set[asyncio.Task] = set()
# The answer's list of numbers: «1, 3 и 7»; a number in a remark after it is not a fact
_NUMBERS = re.compile(r'\d+(?:\s*(?:,|;|и)\s*\d+)*')


def _count(times: dict[int, deque[float]], fact_id: int, now: float) -> int:
    """Marks of the fact inside the window; older ones are dropped."""
    marks = times.get(fact_id)
    if marks is None:
        return 0
    while marks and now - marks[0] > Context.FACT_REST_MINUTES * 60:
        marks.popleft()
    if not marks:
        del times[fact_id]
    return len(marks)


def _sweep(times: dict[int, deque[float]], now: float) -> None:
    """Drop the marks out of the window for every fact: one nobody asks about holds none."""
    for fact_id in list(times):
        _count(times, fact_id, now)


def resting(fact_id: int) -> bool:
    """Whether the fact stays out of the context now. A limit of 0 switches its rule off."""
    now = time.monotonic()
    return ((Context.FACT_SHOWS > 0 and _count(_shown, fact_id, now) >= Context.FACT_SHOWS)
            or (Context.FACT_USES > 0 and _count(_used, fact_id, now) >= Context.FACT_USES))


def answered(text: str, facts: list[Fact]) -> None:
    """An answer built with these facts reached chat: count them shown and check in the
    background which of them it used. Never blocks or raises."""
    if not facts:
        return
    now = time.monotonic()
    if Context.FACT_SHOWS > 0:
        _sweep(_shown, now)
        for fact in facts:
            _shown.setdefault(fact.id, deque()).append(now)
    if Context.FACT_USES > 0:
        _sweep(_used, now)
        task = asyncio.create_task(_check(text, facts))
        _checks.add(task)
        task.add_done_callback(_checks.discard)


async def used(text: str, facts: list[Fact]) -> list[Fact]:
    """The facts the answer used, retold ones too; [] – none, or no answer from Gemini."""
    listing = '\n'.join(f'{i}. {fact.text}' for i, fact in enumerate(facts, 1))
    prompt = Content.prompt('fact_check', answer=text, facts=listing)
    # A background request: it shares the memory's slots, not the ones viewers wait on
    async with background():
        reply = await generate(prompt, make_gen_config(persona=False, temperature=TEMPERATURE))
    if not reply:
        return []
    found = _NUMBERS.search(reply)
    numbers = {int(n) for n in re.findall(r'\d+', found.group())} if found else set()
    return [fact for i, fact in enumerate(facts, 1) if i in numbers]


async def _check(text: str, facts: list[Fact]) -> None:
    try:
        hits = await used(text, facts)
    except Exception:
        logger.exception('Не удалось проверить, какие факты досье ушли в ответ')
        return
    now = time.monotonic()
    for fact in hits:
        _used.setdefault(fact.id, deque()).append(now)
        if resting(fact.id):
            logger.info('Факт досье #%d уходит на отдых: %s', fact.id, fact.text)
