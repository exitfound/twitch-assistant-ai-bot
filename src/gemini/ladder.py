"""The fallback ladder: the same request with less context, one rung at a time.

Gemini's input filter cannot be switched off and judges a request by combinations of
messages, so a blocked request is asked again with less. Free-text answers
(answer_context.py), !who, !versus and !summary build their own rungs and walk them here.
"""
import asyncio
import logging

from google.genai import types

from src.core.config import Gemini
from src.gemini.client import BLOCK_INPUT, BLOCK_OUTPUT, generate_checked

logger = logging.getLogger(__name__)

# (rung name for the log, prompt), richest first
Rung = tuple[str, str]


def unique_rungs(rungs: list[Rung]) -> list[Rung]:
    """Rungs without repeats: with little data several come out the same prompt."""
    unique, seen = [], set()
    for name, prompt in rungs:
        if prompt not in seen:
            seen.add(prompt)
            unique.append((name, prompt))
    return unique


async def walk(rungs: list[Rung], config: types.GenerateContentConfig, user: str) -> tuple[str | None, str]:
    """The answer and the rung that gave it. Walks down the rungs while Gemini's
    input filter blocks the request; the random output filter gets one retry of the
    same rung; an answer with nothing in it or a request the API refused for its content
    jumps to the last rung. No answer at all (timeout, network, a bad key) ends the walk:
    it says nothing about the prompt, and another rung would only double the wait.
    The whole walk fits in Gemini.ANSWER_DEADLINE."""
    reached = [rungs[0][0]]
    try:
        async with asyncio.timeout(Gemini.ANSWER_DEADLINE):
            return await _walk(rungs, config, user, reached)
    except TimeoutError:
        logger.warning('Ответ %s не уложился в %d с, ступень «%s»',
                       user, Gemini.ANSWER_DEADLINE, reached[0])
        return None, reached[0]


async def _walk(rungs: list[Rung], config: types.GenerateContentConfig, user: str,
                reached: list[str]) -> tuple[str | None, str]:
    """walk() without the deadline; reached[0] is the rung being asked."""
    for i, (name, prompt) in enumerate(rungs):
        reached[0] = name
        text, block = await generate_checked(prompt, config)
        if not text and block == BLOCK_OUTPUT:
            # The output filter is random – the same prompt usually passes on the
            # next try, so it keeps its rung
            logger.info('Ответ %s остановлен выходным фильтром на ступени «%s» – повторяю', user, name)
            text, block = await generate_checked(prompt, config)
        if text:
            if i:
                logger.info('Ответ %s получен на ступени «%s»', user, name)
            return text, name
        if block is None:
            logger.warning('Gemini не ответил для %s на ступени «%s»', user, name)
            return None, name
        if i == len(rungs) - 1:
            break
        if block == BLOCK_INPUT:
            logger.info('Запрос %s заблокирован фильтром Gemini на ступени «%s» – беру меньше',
                        user, name)
            continue
        # Answered with nothing (EMPTY, an output block twice) or rejected by the API
        # (ERROR, which may be the size): go straight to the narrowest rung
        logger.warning('Пустой ответ для %s, повтор на последней ступени', user)
        name, prompt = rungs[-1]
        reached[0] = name
        text, _ = await generate_checked(prompt, config)
        return text, name
    return None, rungs[-1][0]
