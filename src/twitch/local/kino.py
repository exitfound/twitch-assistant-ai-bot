"""!кино – the cinema hall on its own site: the link and the current session's code.

The session lives in the hall's gate (`KINO_URL`/_gate/api/session); the bot only asks
it. «!кино старт» and «!кино стоп» are the broadcaster's: a new session with a new code,
and its end, after which the old code no longer opens the site.
"""
import logging

import aiohttp

from src.core.config import Kino
from src.core.content import Content
from src.twitch.core.commands import CommandContext
from src.twitch.core.replies import reply

logger = logging.getLogger(__name__)

TRIGGER = '!кино'
START_TRIGGER = '!кино старт'
STOP_TRIGGER = '!кино стоп'

API_PATH = '/_gate/api/session'

# The gate answers in milliseconds; a hung request must not hold a chat handler
TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)


class GateError(Exception):
    """The gate did not answer, or answered with something other than 200 and JSON."""


async def gate(method: str) -> dict:
    """One call to the gate's session API: GET – the state, POST – a new session, DELETE – the end."""
    headers = {'Authorization': f'Bearer {Kino.API_TOKEN}'}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session, \
                session.request(method, f'{Kino.URL}{API_PATH}', headers=headers) as resp:
            if resp.status != 200:
                raise GateError(f'{method}: HTTP {resp.status}')
            data = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise GateError(f'{method}: {error!r}') from error
    if not isinstance(data, dict):
        raise GateError(f'{method}: не JSON-объект')
    return data


async def handle_kino(ctx: CommandContext) -> None:
    """!кино – the link and the code while a session is open, otherwise «closed»."""
    try:
        state = await gate('GET')
    except GateError as error:
        logger.warning('Кинозал: шлюз не ответил на запрос состояния (%s)', error)
        await reply(ctx.message, Content.text('kino_error', user=ctx.user))
        return
    if not state.get('active'):
        await reply(ctx.message, Content.text('kino_closed', user=ctx.user))
        return
    await reply(ctx.message, Content.text('kino_info', user=ctx.user, url=Kino.URL, code=state.get('code', '')))


async def handle_kino_start(ctx: CommandContext) -> None:
    """!кино старт – a new session; the link and the code go to chat as the bot's own line."""
    try:
        state = await gate('POST')
    except GateError as error:
        logger.warning('Кинозал: сеанс не открылся (%s)', error)
        await reply(ctx.message, Content.text('kino_error', user=ctx.user))
        return
    logger.info('Кинозал: сеанс открыт')
    await ctx.bot.send_chat_message(Content.text('kino_open', url=Kino.URL, code=state.get('code', '')))


async def handle_kino_stop(ctx: CommandContext) -> None:
    """!кино стоп – the session ends, the code stops opening the site."""
    try:
        await gate('DELETE')
    except GateError as error:
        logger.warning('Кинозал: сеанс не закрылся (%s)', error)
        await reply(ctx.message, Content.text('kino_error', user=ctx.user))
        return
    logger.info('Кинозал: сеанс закрыт')
    await ctx.bot.send_chat_message(Content.text('kino_close'))
