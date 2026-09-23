"""chat_messages: saving, the chat windows for context, activity checks and !stat numbers."""
from src.core.db.connection import get_db, transaction


async def save_chat_message(
    session_id: str, username: str, message: str, *, addressed: bool = False,
) -> None:
    """Save a chat message. addressed – whether it was addressed to the bot."""
    async with transaction() as db:
        await db.execute(
            'INSERT INTO chat_messages (session_id, username, message, addressed) VALUES (?, ?, ?, ?)',
            (session_id, username, message, int(addressed)),
        )


async def get_recent_chat(session_id: str, limit: int = 20,
                          before_id: int | None = None) -> list[tuple[str, str]]:
    """The session's last `limit` messages, oldest first. before_id – only older ones
    (the context probe rebuilds the chat as it was at a past question)."""
    db = await get_db()
    where, params = ('', ()) if before_id is None else (' AND id < ?', (before_id,))
    async with db.execute(
        # Commands are not stored any more; the filter covers rows that still hold them
        "SELECT username, message FROM chat_messages WHERE session_id = ? AND message NOT LIKE '!%'"
        f'{where} ORDER BY id DESC LIMIT ?',
        (session_id, *params, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))


# Both lookups scan the whole chat, and every free-text answer and !summary makes one.
# The session before a given one never changes once found; the latest session other than
# a given one changes only when some other session gets a message
_previous_sessions: dict[tuple[str, int], str] = {}
_last_sessions: dict[tuple[str, int], tuple[str | None, int]] = {}
_CACHE_MAX = 64


def _remember(cache: dict, key: tuple, value: object) -> None:
    if len(cache) >= _CACHE_MAX:
        cache.clear()
    cache[key] = value


async def get_previous_chat_session(session_id: str, min_messages: int) -> str | None:
    """The last session before this one with at least min_messages – the previous stream.

    Taken from the chat rather than from streams, which does not cover the older
    sessions; a stream nobody wrote in has nothing to remember anyway.
    """
    key = (session_id, min_messages)
    if key in _previous_sessions:
        return _previous_sessions[key]
    db = await get_db()
    async with db.execute(
        "SELECT session_id FROM chat_messages"
        " WHERE id < (SELECT MIN(id) FROM chat_messages WHERE session_id = ?) AND message NOT LIKE '!%'"
        ' GROUP BY session_id HAVING COUNT(*) >= ? ORDER BY MAX(id) DESC LIMIT 1',
        (session_id, min_messages),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        # Not cached: a session with no chat yet has no «before» to look behind
        return None
    _remember(_previous_sessions, key, row[0])
    return row[0]


async def get_last_chat_session(exclude: str, min_messages: int) -> str | None:
    """The latest session other than this one with at least min_messages – offline,
    when the current session is a date with no chat, that is the last stream."""
    key = (exclude, min_messages)
    db = await get_db()
    if key in _last_sessions:
        found, checked = _last_sessions[key]
        async with db.execute(
            'SELECT EXISTS (SELECT 1 FROM chat_messages WHERE id > ? AND session_id != ?)',
            (checked, exclude),
        ) as cursor:
            if not (await cursor.fetchone())[0]:
                return found
    # The newest id first: a message written during the scan makes the next call look again
    async with db.execute('SELECT COALESCE(MAX(id), 0) FROM chat_messages') as cursor:
        (checked,) = await cursor.fetchone()
    async with db.execute(
        "SELECT session_id FROM chat_messages WHERE session_id != ? AND message NOT LIKE '!%'"
        ' GROUP BY session_id HAVING COUNT(*) >= ? ORDER BY MAX(id) DESC LIMIT 1',
        (exclude, min_messages),
    ) as cursor:
        row = await cursor.fetchone()
    found = row[0] if row else None
    _remember(_last_sessions, key, (found, checked))
    return found


async def get_chat_after(session_id: str, after_id: int) -> int | None:
    """id of the session's last message after after_id. None – nobody has written since.

    Needed by the command reminder: it does not post into a chat that went quiet.
    """
    db = await get_db()
    async with db.execute(
        'SELECT MAX(id) FROM chat_messages WHERE session_id = ? AND id > ?',
        (session_id, after_id),
    ) as cursor:
        (last_id,) = await cursor.fetchone()
    return last_id


async def get_user_messages(username: str, limit: int = 30) -> list[str]:
    db = await get_db()
    async with db.execute(
        # Commands (!roll, !who …) say nothing about a person but eat up the window.
        # They are not written any more; the filter covers rows that still hold them
        "SELECT message FROM chat_messages WHERE username = ? AND message NOT LIKE '!%'"
        ' ORDER BY id DESC LIMIT ?',
        (username, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in reversed(rows)]


async def has_chatted(username: str) -> bool:
    """Whether the nick has ever written in the channel chat, all time.

    Commands are not written to chat_messages, so a viewer who only issues
    commands is also looked up in the rolls and in the Gemini request journal.
    """
    db = await get_db()
    async with db.execute(
        'SELECT EXISTS (SELECT 1 FROM chat_messages WHERE username = ?)'
        ' OR EXISTS (SELECT 1 FROM rolls WHERE username = ?)'
        ' OR EXISTS (SELECT 1 FROM bot_uses WHERE username = ?)',
        (username, username, username),
    ) as cursor:
        return bool((await cursor.fetchone())[0])


async def get_session_stats(session_id: str) -> tuple[int, int]:
    """Messages in the session and addressings of the bot in it."""
    db = await get_db()
    async with db.execute(
        'SELECT COUNT(*), COALESCE(SUM(addressed), 0) FROM chat_messages WHERE session_id = ?',
        (session_id,),
    ) as cursor:
        msgs, interactions = await cursor.fetchone()
    return msgs, interactions


async def get_total_stats() -> tuple[int, int, int, int]:
    """Messages, addressings, streams and off-stream days, all time.

    Session ids come in two kinds and must not be added into one number: a calendar day
    (`YYYY-MM-DD`) and a stream (`YYYY-MM-DD HH:MM`). They are told apart by id length.
    """
    db = await get_db()
    async with db.execute('SELECT COUNT(*), COALESCE(SUM(addressed), 0) FROM chat_messages') as cursor:
        msgs, interactions = await cursor.fetchone()
    async with db.execute(
        'SELECT COALESCE(SUM(LENGTH(session_id) > 10), 0), COALESCE(SUM(LENGTH(session_id) = 10), 0)'
        ' FROM (SELECT DISTINCT session_id FROM chat_messages)'
    ) as cursor:
        streams, days = await cursor.fetchone()
    return msgs, interactions, streams, days


async def get_user_stats(session_id: str, username: str) -> tuple[int, int, int, int] | None:
    """Viewer stats: (session messages, session addressings, all-time messages, all-time addressings).

    None – the nick was never seen (see has_chatted()): nothing to count, almost always a typo.
    """
    db = await get_db()
    async with db.execute(
        'SELECT COUNT(*), COALESCE(SUM(addressed), 0) FROM chat_messages WHERE username = ?',
        (username,),
    ) as cursor:
        total_msgs, total_interactions = await cursor.fetchone()
    # No messages means either a typo or a viewer who only issues commands
    # (commands are not saved): the latter gets honest zeros
    if not total_msgs and not await has_chatted(username):
        return None
    async with db.execute(
        'SELECT COUNT(*), COALESCE(SUM(addressed), 0) FROM chat_messages'
        ' WHERE session_id = ? AND username = ?',
        (session_id, username),
    ) as cursor:
        session_msgs, session_interactions = await cursor.fetchone()
    return session_msgs, session_interactions, total_msgs, total_interactions
