"""streams: Twitch stream id → bot session, start and end."""
import time
from typing import NamedTuple

from src.core.db.connection import get_db


class StreamRow(NamedTuple):
    stream_id: str
    session_id: str
    started_at: float
    ended_at: float | None


async def get_stream(stream_id: str) -> StreamRow | None:
    db = await get_db()
    async with db.execute(
        'SELECT stream_id, session_id, started_at, ended_at FROM streams WHERE stream_id = ?',
        (stream_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return StreamRow(*row) if row else None


async def get_last_stream() -> StreamRow | None:
    db = await get_db()
    async with db.execute(
        'SELECT stream_id, session_id, started_at, ended_at FROM streams ORDER BY started_at DESC LIMIT 1'
    ) as cursor:
        row = await cursor.fetchone()
    return StreamRow(*row) if row else None


async def save_stream(stream_id: str, session_id: str, started_at: float) -> None:
    db = await get_db()
    await db.execute(
        'INSERT OR IGNORE INTO streams (stream_id, session_id, started_at) VALUES (?, ?, ?)',
        (stream_id, session_id, started_at),
    )
    await db.commit()


async def end_stream(stream_id: str, ended_at: float) -> None:
    db = await get_db()
    await db.execute(
        'UPDATE streams SET ended_at = ? WHERE stream_id = ? AND ended_at IS NULL',
        (ended_at, stream_id),
    )
    await db.commit()


async def last_chat_time(session_id: str) -> float | None:
    """time.time() of the last chat message in the session. None – there were no messages."""
    db = await get_db()
    async with db.execute(
        "SELECT CAST(strftime('%s', MAX(created_at)) AS REAL) FROM chat_messages WHERE session_id = ?",
        (session_id,),
    ) as cursor:
        (value,) = await cursor.fetchone()
    return value


async def reopen_stream(stream_id: str) -> None:
    """The stream is live again: it was closed by mistake – a missed event or Twitch lag."""
    db = await get_db()
    await db.execute('UPDATE streams SET ended_at = NULL WHERE stream_id = ?', (stream_id,))
    await db.commit()


async def get_session_start(session_id: str) -> float | None:
    """Start of a stream session: the start of its first stream. None – the session is a date, not a stream."""
    db = await get_db()
    async with db.execute('SELECT MIN(started_at) FROM streams WHERE session_id = ?', (session_id,)) as cursor:
        (value,) = await cursor.fetchone()
    return value


async def get_previous_stream_session(session_id: str) -> str | None:
    """Session of the stream that preceded this one. None – no earlier streams recorded."""
    start = await get_session_start(session_id)
    db = await get_db()
    async with db.execute(
        'SELECT session_id FROM streams WHERE session_id != ? AND started_at < ?'
        ' ORDER BY started_at DESC LIMIT 1',
        (session_id, start if start is not None else time.time()),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None
