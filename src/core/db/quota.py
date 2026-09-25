"""bot_uses: the hourly quota, the channel ceiling and the per-stream limits."""
from src.core.db.connection import get_db, transaction
from src.core.db.streams import get_session_start


async def record_bot_use(username: str, kind: str) -> None:
    """Record a bot request: the hourly quota is counted from this journal."""
    async with transaction() as db:
        await db.execute('INSERT INTO bot_uses (username, kind) VALUES (?, ?)', (username, kind))


async def forget_bot_use(username: str, kind: str) -> None:
    """Remove the last recorded request: the handler rejected the input format.

    The pair of record_bot_use: the dispatcher records the request before calling the
    handler, and a handler that generated nothing gives the quota slot back –
    the same way it gives back the cooldown.
    """
    async with transaction() as db:
        await db.execute(
            'DELETE FROM bot_uses WHERE id = (SELECT MAX(id) FROM bot_uses WHERE username = ? AND kind = ?)',
            (username, kind),
        )


async def count_bot_uses_since(username: str, kind: str, since: float) -> int:
    """How many requests of this kind the viewer made since `since` (time.time()).

    Needed for limits counted per stream rather than over a sliding window:
    the stream start comes from streams, the rest is counted from the journal.
    """
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) FROM bot_uses WHERE username = ? AND kind = ?"
        " AND created_at > datetime(?, 'unixepoch')",
        (username, kind, since),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def count_bot_uses(username: str, kind: str, window_minutes: int) -> int:
    """How many requests of this kind the viewer made in the last window_minutes.

    SQLite does the time math: created_at is written as its CURRENT_TIMESTAMP in UTC.
    """
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) FROM bot_uses WHERE username = ? AND kind = ?"
        " AND created_at > datetime('now', ?)",
        (username, kind, f'-{window_minutes} minutes'),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def count_all_bot_uses(kind: str) -> int:
    """Every row of this kind ever recorded: bot_uses is never pruned, so !clip numbers its clips by it."""
    db = await get_db()
    async with db.execute('SELECT COUNT(*) FROM bot_uses WHERE kind = ?', (kind,)) as cursor:
        return (await cursor.fetchone())[0]


async def count_channel_bot_uses(kind: str, window_minutes: int) -> int:
    """Served requests by everyone in the last window_minutes.

    The per-viewer quota holds one person's volume; this one holds the bill, since every
    badge above follower is unlimited per person. Counted by the dispatcher's kind alone:
    !ask, !who, !versus, !summary and !ascii write a second row each for their own per-stream
    limit, and counting every row would make those commands cost two.
    """
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) FROM bot_uses WHERE kind = ? AND created_at > datetime('now', ?)",
        (kind, f'-{window_minutes} minutes'),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def oldest_bot_use_age(username: str, kind: str, window_minutes: int) -> int | None:
    """How many seconds ago the earliest request inside the window was. None – there are none.

    It tells how soon a slot frees up for a new request.
    """
    db = await get_db()
    async with db.execute(
        "SELECT CAST(strftime('%s','now') - strftime('%s', MIN(created_at)) AS INTEGER)"
        " FROM bot_uses WHERE username = ? AND kind = ? AND created_at > datetime('now', ?)",
        (username, kind, f'-{window_minutes} minutes'),
    ) as cursor:
        (value,) = await cursor.fetchone()
    return value


# Offline the session is a date and there is no stream start: a per-stream limit
# then counts over the last 24 hours
OFFLINE_LIMIT_WINDOW_MINUTES = 24 * 60


async def count_bot_uses_this_stream(username: str, kind: str, session_id: str) -> int:
    """How many requests of this kind the viewer made this stream (offline – in the
    last 24 hours). For per-stream limits: !ascii, !who."""
    start = await get_session_start(session_id)
    if start is None:
        return await count_bot_uses(username, kind, OFFLINE_LIMIT_WINDOW_MINUTES)
    return await count_bot_uses_since(username, kind, start)
