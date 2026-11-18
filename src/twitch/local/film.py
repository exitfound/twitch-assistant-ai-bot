"""!film – the cinema hall on its own site: the link, the current session's code and the film.

The session lives in the hall's gate (`FILM_URL`/_gate/api/session); the bot only asks
it. «!film start», «!film title» and «!film stop» belong to FILM_CONTROL_ACCESS: a new
session with a new code (refused while one is open – a new code would throw every viewer
out), a new title for the open one, and its end. A session left open when the stream ends
is closed by the bot after STREAM_RESUME_MINUTES (close_after_stream).
The session lives only while the bot does: heartbeat_loop() renews the gate's lease, and
the bot closes the session on its way out (close_on_shutdown). The same heartbeat repeats
the link and the code in chat every FILM_REMIND_MINUTES while a session is open (Reminder).
"""
import asyncio
import logging
import time

import aiohttp

from src.core.config import Film, Stream
from src.core.content import Content
from src.twitch.core.commands import CommandContext
from src.twitch.core.port import BotPort
from src.twitch.core.replies import reply

logger = logging.getLogger(__name__)

TRIGGER = '!film'
START_TRIGGER = '!film start'
TITLE_TRIGGER = '!film title'
STOP_TRIGGER = '!film stop'

API_PATH = '/_gate/api/session'
CHAT_PATH = '/_gate/api/chat'

# The gate keeps 200 characters; a chat line has 500 for the link, the code and the rest
TITLE_MAX = 100

# The gate answers in milliseconds; a hung request must not hold a chat handler
TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
# On shutdown the container has 10 s before it is killed, and the chat line comes after
SHUTDOWN_TIMEOUT = aiohttp.ClientTimeout(total=4, connect=2)

# Every gate call renews its lease (GATE_LEASE_SECONDS, 180): a check a minute leaves
# room for two missed ones before the gate closes the session as abandoned
HEARTBEAT_SECONDS = 60


class GateError(Exception):
    """The gate did not answer, or answered with something other than 200 and JSON."""


class NoSession(GateError):
    """A rename while no session is open: the gate answers 409."""


# Whether a session is open, by the gate's last answer about it: the chat bridge runs only then.
# The heartbeat asks once a minute, start and stop change it at once
_open = False


def is_open() -> bool:
    return _open


async def request(method: str, path: str, body: dict | None = None,
                  limit: aiohttp.ClientTimeout = TIMEOUT) -> dict | list:
    """One call to the gate's API with the bot's token: its JSON, or GateError (NoSession on 409)."""
    headers = {'Authorization': f'Bearer {Film.API_TOKEN}'}
    try:
        async with aiohttp.ClientSession(timeout=limit) as session, \
                session.request(method, f'{Film.URL}{path}', headers=headers, json=body) as resp:
            if resp.status == 409:
                raise NoSession(f'{method}: сеанса нет')
            if resp.status != 200:
                raise GateError(f'{method} {path}: HTTP {resp.status}')
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise GateError(f'{method} {path}: {error!r}') from error


async def gate(method: str, title: str | None = None, limit: aiohttp.ClientTimeout = TIMEOUT) -> dict:
    """One call to the session API: GET – the state, POST – a new session, PATCH – a new title, DELETE – the end."""
    global _open
    body = {'title': title} if method in ('POST', 'PATCH') else None
    data = await request(method, API_PATH, body, limit)
    if not isinstance(data, dict):
        raise GateError(f'{method}: не JSON-объект')
    _open = bool(data.get('active'))
    return data


def _title(ctx: CommandContext) -> str | None:
    """The title from the command's args in their original case, squeezed and cut; None – none given."""
    title = ' '.join(ctx.original_args.split())[:TITLE_MAX]
    return title or None


def _same_session(a: dict, b: dict) -> bool:
    """Whether two gate answers are one session: a new one gets a new code and a new start."""
    return (a.get('code'), a.get('started')) == (b.get('code'), b.get('started'))


def _text(key: str, state: dict, **fields) -> str:
    """A line with the link and the code; the `_titled` variant of the key when the session has a title."""
    title = state.get('title')
    if title:
        return Content.text(f'{key}_titled', url=Film.URL, code=state.get('code', ''), title=title, **fields)
    return Content.text(key, url=Film.URL, code=state.get('code', ''), **fields)


async def handle_film(ctx: CommandContext) -> None:
    """!film – the link, the code and the film while a session is open, otherwise «closed»."""
    try:
        state = await gate('GET')
    except GateError as error:
        logger.warning('Кинозал: шлюз не ответил на запрос состояния (%s)', error)
        await reply(ctx.message, Content.text('film_error', user=ctx.user))
        return
    if not state.get('active'):
        await reply(ctx.message, Content.text('film_closed', user=ctx.user))
        return
    await reply(ctx.message, _text('film_info', state, user=ctx.user))


async def handle_film_start(ctx: CommandContext) -> None:
    """!film start [title] – a new session; the link, the code and the film go to chat as the bot's own line.

    While a session is open it is refused: the gate would give a new code and every
    viewer would be thrown back to the code page. A new film is «!film title».
    """
    try:
        current = await gate('GET')
        if current.get('active'):
            ctx.clear_cooldown()
            await reply(ctx.message, Content.text('film_already_open', user=ctx.user, code=current.get('code', ''),
                                                  title_command=TITLE_TRIGGER, stop_command=STOP_TRIGGER))
            return
        state = await gate('POST', _title(ctx))
    except GateError as error:
        logger.warning('Кинозал: сеанс не открылся (%s)', error)
        await reply(ctx.message, Content.text('film_error', user=ctx.user))
        return
    logger.info('Кинозал: сеанс открыт (%s)', state.get('title') or 'без названия')
    await ctx.bot.send_chat_message(_text('film_open', state))


async def handle_film_title(ctx: CommandContext) -> None:
    """!film title <title> – a new title for the open session; the code stays."""
    title = _title(ctx)
    if title is None:
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('film_title_usage', user=ctx.user, command=TITLE_TRIGGER))
        return
    try:
        state = await gate('PATCH', title)
    except NoSession:
        await reply(ctx.message, Content.text('film_closed', user=ctx.user))
        return
    except GateError as error:
        logger.warning('Кинозал: название не сменилось (%s)', error)
        await reply(ctx.message, Content.text('film_error', user=ctx.user))
        return
    logger.info('Кинозал: название – %s', state.get('title'))
    await ctx.bot.send_chat_message(Content.text('film_retitled', title=state.get('title') or title))


async def handle_film_stop(ctx: CommandContext) -> None:
    """!film stop – the session ends, the code stops opening the site."""
    try:
        state = await gate('DELETE')
    except GateError as error:
        logger.warning('Кинозал: сеанс не закрылся (%s)', error)
        await reply(ctx.message, Content.text('film_error', user=ctx.user))
        return
    # The gate answers 200 with nothing to close too: then there is no film to say goodbye to
    if not state.get('closed'):
        await reply(ctx.message, Content.text('film_stop_none', user=ctx.user))
        return
    logger.info('Кинозал: сеанс закрыт')
    await ctx.bot.send_chat_message(Content.text('film_close'))


async def close_after_stream(bot: BotPort) -> None:
    """The stream ended: the session open at that moment is closed once a resume is ruled out.

    The wait is STREAM_RESUME_MINUTES – a stream back within it is the same stream, and
    the bot cancels this task on stream start. The session is remembered at the end and
    compared by its code and start, not by clocks: a session opened after the end (a film
    off stream) is a different one and is left alone, whatever the gate's clock says.
    """
    try:
        at_end = await gate('GET')
    except GateError as error:
        logger.warning('Кинозал: шлюз не ответил в конце эфира, сеанс сам не закроется (%s)', error)
        return
    if not at_end.get('active'):
        return
    await asyncio.sleep(Stream.RESUME_MINUTES * 60)
    if bot.stream_live:
        return
    try:
        state = await gate('GET')
        if not state.get('active') or not _same_session(state, at_end):
            return
        await gate('DELETE')
    except GateError as error:
        logger.warning('Кинозал: сеанс после конца эфира не закрылся (%s)', error)
        return
    logger.info('Кинозал: эфир закончился – сеанс закрыт')
    await bot.send_chat_message(Content.text('film_auto_close'))


class Reminder:
    """The link and the code again every FILM_REMIND_MINUTES of an open session.

    Fed the heartbeat's answers. A new session (a new code or start) restarts the count –
    the start announced it itself; a closed one stops it. The clock is the bot's monotonic
    one, so the gate's clock and a wrong system time do not matter; a gate silent for longer
    than the interval gives one reminder when it is back, not a burst.
    """

    def __init__(self) -> None:
        self.session: tuple | None = None
        self.since = 0.0

    async def check(self, bot: BotPort, state: dict) -> None:
        if not Film.REMIND_MINUTES or not state.get('active'):
            self.session = None
            return
        key = (state.get('code'), state.get('started'))
        now = time.monotonic()
        if key != self.session:
            self.session, self.since = key, now
            return
        every = Film.REMIND_MINUTES * 60
        if now - self.since < every:
            return
        self.since = self.since + every if now - self.since < 2 * every else now
        try:
            await bot.send_chat_message(_text('film_remind', state))
        except Exception as error:  # a chat hiccup must not stop the heartbeat: the session hangs on it
            logger.warning('Кинозал: напоминание не ушло в чат (%r)', error)


async def heartbeat_loop(bot: BotPort) -> None:
    """Tell the gate the bot is alive once a minute; without it the gate ends the session.

    Each answer also goes to the Reminder: an open session's link and code are repeated in chat.
    """
    failing = False
    reminder = Reminder()
    while True:
        try:
            state = await gate('GET')
        except GateError as error:
            if not failing:
                logger.warning('Кинозал: шлюз не отвечает на отметку бота (%s)', error)
            failing = True
        else:
            if failing:
                logger.info('Кинозал: шлюз снова отвечает')
            failing = False
            await reminder.check(bot, state)
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def close_on_shutdown(bot: BotPort) -> None:
    """The bot is going down: the session ends with it, and chat is told if one was open."""
    try:
        state = await gate('DELETE', limit=SHUTDOWN_TIMEOUT)
    except GateError as error:
        logger.warning('Кинозал: сеанс при выключении бота не закрылся (%s)', error)
        return
    if state.get('closed'):
        logger.info('Кинозал: бот выключается – сеанс закрыт')
        await bot.send_chat_message(Content.text('film_shutdown_close'))
