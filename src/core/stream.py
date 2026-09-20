"""The bot's session is the stream, not the calendar day.

While a stream is live the session is the stream and holds chat, stats, summary,
Gemini context and the game; off stream it is the current date and the game is closed.
The streams table binds Twitch's stream id to the session, so a restart mid-stream
continues the same one, and a reconnect within STREAM_RESUME_MINUTES keeps the session
under a new id.

The state comes from stream.online / stream.offline events, a direct Twitch query at
startup, and a watch_stream() check every CHECK_SECONDS that catches events lost to a
websocket drop or a failed subscription. An end the bot did not see is taken from the
Twitch recording, or from the last recorded chat message, and that time decides whether
the next stream counts as an outage.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from src.core.config import Stream
from src.core.database import (
    StreamRow, end_stream, get_last_stream, get_stream, last_chat_time, reopen_stream, save_stream,
)

logger = logging.getLogger(__name__)

# Twitch check: how often, and for how many checks in a row a disagreement must
# hold before the bot believes it. Twitch's stream list lags behind events by a
# minute or two at the start and end of a stream, so one check is not enough
CHECK_SECONDS = 120
CONFIRMATIONS = 3

# (stream_id, started_at) → time.time() of the stream end, or None if there is no way to tell
EndLookup = Callable[[str, float], Awaitable[float | None]]


def _session_name(started_at: float) -> str:
    # Start date and time in local time: readable in !stat and sorts
    # after date sessions of the same day
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(started_at))


class StreamTracker:

    def __init__(self, end_lookup: EndLookup | None = None) -> None:
        self._end_lookup = end_lookup
        self._stream_id: str | None = None
        self._session: str | None = None

    @property
    def live(self) -> bool:
        return self._session is not None

    @property
    def stream_id(self) -> str | None:
        return self._stream_id

    @property
    def session_id(self) -> str:
        return self._session or time.strftime('%Y-%m-%d')

    async def online(self, stream_id: str, started_at: float) -> bool:
        """Stream is live. True – a new session started, False – the previous one continues.

        A known id is a bot restart, a redelivered event or a stream that was
        closed by mistake. A new id shortly after the previous stream ended is an
        outage, same session. A previous stream whose end the bot did not see is closed.
        """
        known = await get_stream(stream_id)
        if known is not None:
            if known.ended_at is not None:
                await reopen_stream(stream_id)
                logger.warning('Эфир %s снова идёт – его закрыли по ошибке, открываю', stream_id)
            session, new = known.session_id, False
        else:
            session, new = _session_name(started_at), True
            last = await get_last_stream()
            if last is not None:
                ended = last.ended_at
                if ended is None:
                    ended = min(await self._estimate_end(last), started_at)
                    await end_stream(last.stream_id, ended)
                if Stream.RESUME_MINUTES and started_at - ended <= Stream.RESUME_MINUTES * 60:
                    session, new = last.session_id, False
            await save_stream(stream_id, session, started_at)
        self._stream_id, self._session = stream_id, session
        logger.info('Эфир %s: сессия %s (%s)', stream_id, session, 'новая' if new else 'продолжается')
        return new

    async def offline(self) -> None:
        """Stream ended: the session is by date again."""
        if self._stream_id is not None:
            await end_stream(self._stream_id, time.time())
            logger.info('Эфир %s закончился, сессия %s закрыта', self._stream_id, self._session)
        self._stream_id = self._session = None

    async def settle_missed_end(self) -> None:
        """No stream at startup, but the last one in the DB is open: the bot missed its end."""
        last = await get_last_stream()
        if last is None or last.ended_at is not None:
            return
        ended = await self._estimate_end(last)
        await end_stream(last.stream_id, ended)
        logger.info('Эфир %s закончился, пока бота не было: конец записан на %s',
                    last.stream_id, time.strftime('%Y-%m-%d %H:%M', time.localtime(ended)))

    async def _estimate_end(self, stream: StreamRow) -> float:
        """When a stream ended whose end the bot did not see.

        Exactly – from the stream's recording on Twitch. No recording (VODs off) –
        from the last chat message of the session the bot managed to record, roughly
        the moment it went down. In the worst case – the stream start.
        """
        if self._end_lookup is not None:
            try:
                ended = await self._end_lookup(stream.stream_id, stream.started_at)
            except Exception:
                logger.warning('Не удалось узнать конец эфира %s по записи', stream.stream_id, exc_info=True)
                ended = None
            if ended is not None:
                return ended
        return await last_chat_time(stream.session_id) or stream.started_at


async def watch_stream(bot) -> None:
    """Check against Twitch every CHECK_SECONDS: catches a missed stream start or end.

    bot must provide fetch_live_stream(), stream_went_online() and
    stream_went_offline(). The bot believes a disagreement with Twitch only if it
    holds for CONFIRMATIONS checks in a row and points to the same thing.
    """
    seen: str | None = None
    strikes = 0
    try:
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            try:
                observed = await bot.fetch_live_stream()
            except Exception:
                logger.warning('Сверка эфира с Twitch не удалась', exc_info=True)
                continue
            key = observed[0] if observed else None
            if key == bot.stream.stream_id:
                seen, strikes = None, 0
                continue
            strikes = strikes + 1 if strikes and key == seen else 1
            seen = key
            if strikes < CONFIRMATIONS:
                continue
            seen, strikes = None, 0
            try:
                if observed is None:
                    logger.warning('Конец эфира пропущен – закрываю сессию по сверке с Twitch')
                    await bot.stream_went_offline()
                else:
                    logger.warning('Начало эфира %s пропущено – открываю по сверке с Twitch', key)
                    await bot.stream_went_online(*observed)
            except Exception:
                logger.exception('Состояние эфира по сверке не применено')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Сверка эфира остановлена')
