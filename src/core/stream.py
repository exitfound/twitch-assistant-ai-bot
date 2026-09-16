"""Сессия бота — это эфир, а не календарный день.

Пока идёт стрим, сессия — это эфир: чат, статистика, саммари, контекст Gemini
и игра живут в её рамках. id эфира от Twitch записан в таблицу streams вместе
с сессией, поэтому перезапуск бота посреди стрима продолжает ту же сессию.
Короткий обрыв (стример переподключился за STREAM_RESUME_MINUTES) даёт новый
id, но сессия остаётся прежней.

Пока эфира нет, сессия — текущая дата, как было раньше: чат пишется и вне
стрима, а игра в это время закрыта.

Откуда бот знает про эфир:
- события stream.online / stream.offline приходят сразу;
- при запуске бот сам спрашивает Twitch, идёт ли стрим: события о том, что
  случилось, пока бот был выключен, уже не придут;
- раз в CHECK_SECONDS watch_stream() сверяется с Twitch и ловит события,
  которые потерялись (разрыв вебсокета, неудачная подписка, ошибка на старте).

Конец эфира, который бот не видел (был выключен), берётся по записи эфира в
Twitch, а если записи нет — по последнему сообщению чата, которое бот успел
записать. От этого времени зависит, будет ли следующий эфир считаться обрывом.
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

# Сверка с Twitch: как часто и сколько проверок подряд расхождение должно
# держаться, прежде чем бот ему поверит. Список эфиров в Twitch отстаёт от
# событий на минуту-другую в начале и в конце стрима, одной проверки мало
CHECK_SECONDS = 120
CONFIRMATIONS = 3

# (stream_id, started_at) → time.time() конца эфира или None, если узнать нечем
EndLookup = Callable[[str, float], Awaitable[float | None]]


def _session_name(started_at: float) -> str:
    # Дата и время начала по местным часам: читается в !stat и сортируется
    # после сессий-дат того же дня
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
        """Эфир идёт. True — началась новая сессия, False — продолжается прежняя.

        Знакомый id — это перезапуск бота, повтор события или эфир, который
        закрыли по ошибке. Новый id вскоре после конца прошлого эфира — обрыв,
        сессия та же. Прошлый эфир, конец которого бот не видел, закрывается.
        """
        known = await get_stream(stream_id)
        if known is not None:
            if known.ended_at is not None:
                await reopen_stream(stream_id)
                logger.warning('Эфир %s снова идёт — его закрыли по ошибке, открываю', stream_id)
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
        """Эфир закончился: сессия снова по дате."""
        if self._stream_id is not None:
            await end_stream(self._stream_id, time.time())
            logger.info('Эфир %s закончился, сессия %s закрыта', self._stream_id, self._session)
        self._stream_id = self._session = None

    async def settle_missed_end(self) -> None:
        """На старте эфира нет, а последний в базе не закрыт: бот не видел его конца."""
        last = await get_last_stream()
        if last is None or last.ended_at is not None:
            return
        ended = await self._estimate_end(last)
        await end_stream(last.stream_id, ended)
        logger.info('Эфир %s закончился, пока бота не было: конец записан на %s',
                    last.stream_id, time.strftime('%Y-%m-%d %H:%M', time.localtime(ended)))

    async def _estimate_end(self, stream: StreamRow) -> float:
        """Когда кончился эфир, конец которого бот не видел.

        Точно — по записи эфира в Twitch. Записи нет (VOD выключены) — по
        последнему сообщению чата в сессии, которое бот успел записать, то есть
        примерно по моменту, когда бот выключился. В худшем случае — начало эфира.
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
    """Сверка с Twitch раз в CHECK_SECONDS: ловит пропущенные начало и конец эфира.

    bot должен уметь fetch_live_stream(), stream_went_online() и
    stream_went_offline(). Расхождению с Twitch бот верит, только если оно
    держится CONFIRMATIONS проверок подряд и указывает на одно и то же.
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
                    logger.warning('Конец эфира пропущен — закрываю сессию по сверке с Twitch')
                    await bot.stream_went_offline()
                else:
                    logger.warning('Начало эфира %s пропущено — открываю по сверке с Twitch', key)
                    await bot.stream_went_online(*observed)
            except Exception:
                logger.exception('Состояние эфира по сверке не применено')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Сверка эфира остановлена')
