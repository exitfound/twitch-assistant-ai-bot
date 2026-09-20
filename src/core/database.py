import asyncio
import random
import time
import aiosqlite
import logging
from pathlib import Path
from typing import NamedTuple

from src.core.config import Context
# DB_PATH may be moved by BOT_DB_PATH – see src/core/paths.py. It is re-exported
# under this module's name because every caller imports get_db(), not the path
from src.core.paths import DB_PATH
from src.core.utils import SOSUR_RE

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
# Ids of all knowledge rows for the random «language» sample, and when they were read
_knowledge_ids: list[int] | None = None
_knowledge_ids_at = 0.0
# The CLI imports into the same file while the bot runs, and cannot reset the bot's
# cache: the ids are re-read this often, so new lore reaches the sample on its own
KNOWLEDGE_IDS_TTL = 600


async def get_db() -> aiosqlite.Connection:
    global _db
    async with _db_lock:
        if _db is None:
            _db = await aiosqlite.connect(DB_PATH)
            await _db.execute('PRAGMA journal_mode=WAL')
            await _db.execute('PRAGMA synchronous=NORMAL')
    return _db


async def close_db() -> None:
    global _db
    async with _db_lock:
        if _db is not None:
            await _db.close()
            _db = None


async def init_db() -> None:
    db = await get_db()
    await db.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            username   TEXT NOT NULL,
            message    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            addressed  INTEGER NOT NULL DEFAULT 0
        )
    ''')
    await _migrate_chat_messages(db)
    await db.execute('''
        CREATE TABLE IF NOT EXISTS bot_interactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            username     TEXT NOT NULL,
            user_message TEXT NOT NULL,
            bot_response TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS facts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            fact       TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, fact)
        )
    ''')
    # source – where a row came from (a file, a Telegram chat, 'facts'), so one
    # source can be removed whole with --clear-lore --source. Rows imported before
    # the column (2026-09-19) have NULL and stay as they are
    await db.execute('''
        CREATE TABLE IF NOT EXISTS knowledge (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source     TEXT
        )
    ''')
    if 'source' not in await _columns(db, 'knowledge'):
        # Additive: the running bot's queries never name the column
        await db.execute('ALTER TABLE knowledge ADD COLUMN source TEXT')
        logger.warning('В knowledge добавлена колонка source')
    await db.execute('CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge(source)')
    # Journal of Gemini requests: the viewer's hourly quota is counted from it.
    # In the DB, not in memory, so a bot restart does not reset the limits
    await db.execute('''
        CREATE TABLE IF NOT EXISTS bot_uses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            kind       TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_bot_uses_user_time ON bot_uses(username, created_at)'
    )
    # Tables of the !roll game: rolls, rewards, roll_actions, roll_perks. Schema and migrations
    # live here with all the others, their queries – in src/local/roll/storage.py
    await db.execute('''
        CREATE TABLE IF NOT EXISTS rolls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            username    TEXT NOT NULL,
            roll_value  INTEGER NOT NULL,
            rolled_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            free_throws INTEGER NOT NULL DEFAULT 0,
            curse_ceiling  INTEGER,
            curse_floor_at REAL,
            curse_until    REAL,
            free_limit     INTEGER,
            UNIQUE(session_id, username)
        )
    ''')
    await _migrate_rolls(db)
    # Which channel-points rewards the bot has already created in Twitch: the id is
    # needed so that renaming a reward does not spawn duplicates
    await db.execute('''
        CREATE TABLE IF NOT EXISTS rewards (
            action    TEXT PRIMARY KEY,
            reward_id TEXT NOT NULL
        )
    ''')
    # Journal of redeemed rewards: who, whom, with what, what it was and what it became.
    # It also guards against reprocessing and stores shields – a shield is a successful entry
    await db.execute('''
        CREATE TABLE IF NOT EXISTS roll_actions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            redemption_id TEXT NOT NULL UNIQUE,
            session_id    TEXT NOT NULL,
            action        TEXT NOT NULL,
            actor         TEXT NOT NULL,
            user_input    TEXT NOT NULL DEFAULT '',
            target        TEXT,
            old_value     INTEGER,
            new_value     INTEGER,
            status        TEXT NOT NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_roll_actions_target ON roll_actions(session_id, target, action)'
    )
    # Game perks from the previous stream's results: a shield for the «китежанин»,
    # a curse for the «залупа». active_from – first appearance in the stream, the
    # countdown starts from it
    await db.execute('''
        CREATE TABLE IF NOT EXISTS roll_perks (
            session_id   TEXT NOT NULL,
            username     TEXT NOT NULL,
            perk         TEXT NOT NULL,
            from_session TEXT NOT NULL,
            active_from  REAL,
            active_until REAL,
            consumed     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, username, perk)
        )
    ''')
    # Channel streams. The bot session is the stream, not the calendar day: the stream id
    # is bound to the session, so a restart mid-stream continues the same one.
    # A short outage gives a new id but the same session
    await db.execute('''
        CREATE TABLE IF NOT EXISTS streams (
            stream_id  TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at   REAL
        )
    ''')
    # The bot's memory of chatters, written by Gemini after a conversation – chat
    # between two long silences (src/gemini/memory/). conversation is its key, the
    # Moscow time of its first message. A chronicle row covers the messages
    # first_id..last_id: status 'ok', 'failed' when Gemini returned nothing (not
    # retried on every start) or 'skipped' when there were too few messages
    await _migrate_memory(db)
    await db.execute('''
        CREATE TABLE IF NOT EXISTS chronicles (
            conversation  TEXT PRIMARY KEY,
            text          TEXT NOT NULL,
            status        TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            first_id      INTEGER NOT NULL DEFAULT 0,
            last_id       INTEGER NOT NULL DEFAULT 0
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS chatter_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation TEXT NOT NULL,
            username     TEXT NOT NULL,
            event        TEXT NOT NULL
        )
    ''')
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_chatter_events_user ON chatter_events(username, conversation)'
    )
    # The memory's own state: 'built' is set once bot.py --build-memory finished the
    # whole history. Until then the bot leaves the memory alone – a half-built one
    # (build killed or still running) would otherwise be replayed conversation by
    # conversation at many times the cost. A DB whose memory predates this table
    # was built completely, so it is marked right away
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name = 'memory_state'") as cursor:
        state_existed = await cursor.fetchone() is not None
    await db.execute('''
        CREATE TABLE IF NOT EXISTS memory_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    if not state_existed:
        await db.execute(
            "INSERT OR IGNORE INTO memory_state (key, value)"
            " SELECT 'built', datetime('now') WHERE EXISTS (SELECT 1 FROM chronicles)"
        )
    # One profile per chatter, rewritten (not appended to) after every conversation
    # they were active in. relations is JSON: [{"nick": ..., "note": ...}].
    # sessions_seen counts bot sessions (streams, or days before streams were tracked)
    await db.execute('''
        CREATE TABLE IF NOT EXISTS chatter_profiles (
            username          TEXT PRIMARY KEY,
            portrait          TEXT NOT NULL,
            relations         TEXT NOT NULL,
            last_conversation TEXT NOT NULL,
            sessions_seen     INTEGER NOT NULL,
            updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_content ON knowledge(content)'
    )
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)'
    )
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_bot_interactions_session ON bot_interactions(session_id)'
    )
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_chat_messages_username ON chat_messages(username)'
    )
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_bot_interactions_username ON bot_interactions(username)'
    )
    # Live DBs created before UNIQUE(username, fact) existed rely on this
    # index – always create it, so the dedup guarantee does not depend on the DB's age.
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_username_fact ON facts(username, fact)'
    )
    await _drop_legacy_objects(db)
    await _migrate_fts(db)
    await _create_fts_triggers(db)
    await db.commit()


_ROLLS_COLUMNS = {
    # Free throw counter. Old rows get 0: past sessions are closed,
    # and in the current one everybody starts with the full allowance
    'free_throws': 'INTEGER NOT NULL DEFAULT 0',
    # Curse: ceiling of the next throw (NULL – not cursed) and the time.time()
    # moment the ceiling reached the floor – the lift is counted from it
    'curse_ceiling': 'INTEGER',
    'curse_floor_at': 'REAL',
    # Hard curse deadline by time.time(): the curse of the previous stream's «залупа»
    # has one, a bought curse has NULL
    'curse_until': 'REAL',
    # How many free throws this player is entitled to in this session: it depends
    # on their status, and a channel-points redemption carries no badges in the event
    'free_limit': 'INTEGER',
}


async def _migrate_chat_messages(db: aiosqlite.Connection) -> None:
    """Column addressed: whether the message was addressed to the bot.

    Addressing is a сосур*/secur* word, a mention of the bot's @nick or a reply to
    its message; the flag is set by the dispatcher, which determines all that anyway.
    History is marked once by text: the word is visible in a message, but replies and
    mentions of past sessions cannot be recovered, so the old numbers are
    a lower bound.
    """
    async with db.execute('PRAGMA table_info(chat_messages)') as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if 'addressed' in columns:
        return
    await db.execute('ALTER TABLE chat_messages ADD COLUMN addressed INTEGER NOT NULL DEFAULT 0')
    async with db.execute('SELECT id, message FROM chat_messages') as cursor:
        rows = await cursor.fetchall()
    hits = [(row_id,) for row_id, message in rows if SOSUR_RE.search(message or '')]
    if hits:
        await db.executemany('UPDATE chat_messages SET addressed = 1 WHERE id = ?', hits)
    logger.warning(
        'В chat_messages добавлена колонка addressed; по истории помечено обращений: %d из %d',
        len(hits), len(rows),
    )


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f'PRAGMA table_info({table})') as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def _migrate_memory(db: aiosqlite.Connection) -> None:
    """The first memory (2026-09-19) went by bot sessions and was named so.

    It now goes by conversations – chat between silences – and the names say
    that: stream_chronicles → chronicles, session_id → conversation,
    updated_session → last_conversation. Chronicles written per session get the
    range of their session's messages, so the memory does not take them again.
    Runs before the CREATE TABLEs, which would otherwise make empty new tables.
    """
    # Every step checks its own result, so a process killed halfway (ALTER TABLE
    # commits on its own) resumes on the next start instead of failing
    old = await _columns(db, 'stream_chronicles')
    if old:
        for name in ('first_id', 'last_id'):
            if name not in old:
                await db.execute(f'ALTER TABLE stream_chronicles ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
        key = 'session_id' if 'session_id' in old else 'conversation'
        await db.execute(f'''
            UPDATE stream_chronicles SET
                first_id = COALESCE((SELECT MIN(id) FROM chat_messages m
                                     WHERE m.session_id = stream_chronicles.{key}), 0),
                last_id  = COALESCE((SELECT MAX(id) FROM chat_messages m
                                     WHERE m.session_id = stream_chronicles.{key}), 0)
            WHERE last_id = 0
        ''')
        await db.commit()
        if key == 'session_id':
            await db.execute('ALTER TABLE stream_chronicles RENAME COLUMN session_id TO conversation')
        await db.execute('ALTER TABLE stream_chronicles RENAME TO chronicles')
        logger.warning('Память: stream_chronicles переименована в chronicles')
    if 'session_id' in await _columns(db, 'chatter_events'):
        # The index on (username, session_id) follows the column by itself
        await db.execute('ALTER TABLE chatter_events RENAME COLUMN session_id TO conversation')
        logger.warning('Память: chatter_events.session_id → conversation')
    if 'updated_session' in await _columns(db, 'chatter_profiles'):
        await db.execute('ALTER TABLE chatter_profiles RENAME COLUMN updated_session TO last_conversation')
        logger.warning('Память: chatter_profiles.updated_session → last_conversation')


async def _migrate_rolls(db: aiosqlite.Connection) -> None:
    """Columns added to rolls after the table itself."""
    async with db.execute('PRAGMA table_info(rolls)') as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    for name, ddl in _ROLLS_COLUMNS.items():
        if name not in columns:
            await db.execute(f'ALTER TABLE rolls ADD COLUMN {name} {ddl}')
            logger.warning('В rolls добавлена колонка %s', name)


# Leftovers of the previous version: the bot_interactions index is no longer read,
# but its triggers kept writing to it on every interaction.
_LEGACY_TRIGGERS = ('bot_interactions_fts_ai', 'bot_interactions_fts_ad', 'bot_interactions_fts_au')
_LEGACY_TABLES = ('interactions_fts',)


async def _drop_legacy_objects(db: aiosqlite.Connection) -> None:
    for trigger in _LEGACY_TRIGGERS:
        await db.execute(f'DROP TRIGGER IF EXISTS {trigger}')
    for table in _LEGACY_TABLES:
        async with db.execute(
            'SELECT 1 FROM sqlite_master WHERE name = ?', (table,)
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await db.execute(f'DROP TABLE IF EXISTS {table}')
            logger.warning('Удалён неиспользуемый legacy-объект: %s', table)


_FTS_TABLES = [
    ('chat_fts', 'chat_messages', ['username', 'message']),
    ('knowledge_fts', 'knowledge', ['content']),
]


async def _migrate_fts(db: aiosqlite.Connection) -> None:
    """Migrate FTS tables to content-linked (content=) if needed."""
    for fts_name, source, columns in _FTS_TABLES:
        needs_migration = False
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (fts_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                needs_migration = True
            elif 'content=' not in row[0]:
                needs_migration = True

        if not needs_migration:
            continue

        cols = ', '.join(columns)
        await db.execute(f'DROP TABLE IF EXISTS {fts_name}')
        await db.execute(
            f"CREATE VIRTUAL TABLE {fts_name} USING fts5("
            f"{cols}, content={source}, content_rowid=id, tokenize='unicode61')"
        )
        await db.execute(f"INSERT INTO {fts_name}({fts_name}) VALUES('rebuild')")
        logger.warning('Migrated %s to content-linked FTS (rebuilt from %s)', fts_name, source)


async def _create_fts_triggers(db: aiosqlite.Connection) -> None:
    """Create triggers that keep FTS indexes in sync with source tables."""
    for fts_name, source, columns in _FTS_TABLES:
        cols = ', '.join(columns)
        cols_new = ', '.join(f'new.{c}' for c in columns)
        cols_old = ', '.join(f'old.{c}' for c in columns)

        await db.execute(f'''
            CREATE TRIGGER IF NOT EXISTS {source}_fts_ai AFTER INSERT ON {source} BEGIN
                INSERT INTO {fts_name}(rowid, {cols}) VALUES (new.id, {cols_new});
            END
        ''')
        await db.execute(f'''
            CREATE TRIGGER IF NOT EXISTS {source}_fts_ad AFTER DELETE ON {source} BEGIN
                INSERT INTO {fts_name}({fts_name}, rowid, {cols}) VALUES('delete', old.id, {cols_old});
            END
        ''')
        await db.execute(f'''
            CREATE TRIGGER IF NOT EXISTS {source}_fts_au AFTER UPDATE ON {source} BEGIN
                INSERT INTO {fts_name}({fts_name}, rowid, {cols}) VALUES('delete', old.id, {cols_old});
                INSERT INTO {fts_name}(rowid, {cols}) VALUES (new.id, {cols_new});
            END
        ''')


async def save_chat_message(
    session_id: str, username: str, message: str, *, addressed: bool = False,
) -> None:
    """Save a chat message. addressed – whether it was addressed to the bot."""
    db = await get_db()
    await db.execute(
        'INSERT INTO chat_messages (session_id, username, message, addressed) VALUES (?, ?, ?, ?)',
        (session_id, username, message, int(addressed)),
    )
    await db.commit()


async def save_bot_interaction(session_id: str, username: str, user_message: str, bot_response: str) -> None:
    db = await get_db()
    await db.execute(
        'INSERT INTO bot_interactions (session_id, username, user_message, bot_response) VALUES (?, ?, ?, ?)',
        (session_id, username, user_message, bot_response),
    )
    await db.commit()


def _contains_ci(haystack: str, needle: str) -> bool:
    """Case-insensitive substring search.

    SQLite LIKE folds case only for ASCII, so Cyrillic is compared
    on the Python side: the facts table is small.
    """
    return needle.casefold() in haystack.casefold()


async def get_relevant_facts(username: str, query: str) -> list[tuple[str, str]]:
    db = await get_db()
    async with db.execute(
        'SELECT username, fact FROM facts WHERE username = ? ORDER BY id',
        (username,),
    ) as cursor:
        user_facts = await cursor.fetchall()

    words = [w for w in query.split() if len(w) > 3]
    if not words:
        return list(user_facts)

    async with db.execute(
        'SELECT username, fact FROM facts WHERE username != ? ORDER BY id', (username,)
    ) as cursor:
        other_facts = await cursor.fetchall()

    result = list(user_facts)
    seen = set(user_facts)
    for row in other_facts:
        if row in seen:
            continue
        if any(_contains_ci(row[1], word) for word in words):
            seen.add(row)
            result.append(row)
    return result


async def get_recent_chat(session_id: str, limit: int = 20,
                          before_id: int | None = None) -> list[tuple[str, str]]:
    """The session's last `limit` messages, oldest first. before_id – only older ones
    (the context probe rebuilds the chat as it was at a past question)."""
    db = await get_db()
    where, params = ('', ()) if before_id is None else (' AND id < ?', (before_id,))
    async with db.execute(
        # Commands are no longer stored; the filter is for the rows written before that
        "SELECT username, message FROM chat_messages WHERE session_id = ? AND message NOT LIKE '!%'"
        f'{where} ORDER BY id DESC LIMIT ?',
        (session_id, *params, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))


async def get_previous_chat_session(session_id: str, min_messages: int) -> str | None:
    """The last session before this one with at least min_messages – the previous stream.

    Taken from the chat rather than from streams: that table only exists since
    2026-09-17, and a stream nobody wrote in has nothing to remember anyway.
    """
    db = await get_db()
    async with db.execute(
        "SELECT session_id FROM chat_messages"
        " WHERE id < (SELECT MIN(id) FROM chat_messages WHERE session_id = ?) AND message NOT LIKE '!%'"
        ' GROUP BY session_id HAVING COUNT(*) >= ? ORDER BY MAX(id) DESC LIMIT 1',
        (session_id, min_messages),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def get_last_chat_session(exclude: str, min_messages: int) -> str | None:
    """The latest session other than this one with at least min_messages – offline,
    when the current session is a date with no chat, that is the last stream."""
    db = await get_db()
    async with db.execute(
        "SELECT session_id FROM chat_messages WHERE session_id != ? AND message NOT LIKE '!%'"
        ' GROUP BY session_id HAVING COUNT(*) >= ? ORDER BY MAX(id) DESC LIMIT 1',
        (exclude, min_messages),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


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
        # New ones are no longer written to the DB, the filter is for old rows
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


async def get_last_tagged_interaction(
    username: str, tag: str, window_minutes: int,
) -> tuple[str, str] | None:
    """The viewer's last exchange with the bot on command tag within window_minutes.

    Returns (question without the tag, answer) or None. Needed by !ask for follow-ups:
    the model cannot make sense of «а подробнее?» without the previous question.
    """
    prefix = f'{tag} '
    db = await get_db()
    async with db.execute(
        "SELECT user_message, bot_response FROM bot_interactions"
        " WHERE username = ? AND substr(user_message, 1, ?) = ?"
        " AND created_at > datetime('now', ?) ORDER BY id DESC LIMIT 1",
        (username, len(prefix), prefix, f'-{window_minutes} minutes'),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row[0][len(prefix):], row[1]


async def get_user_interactions(username: str, limit: int = 10) -> list[tuple[str, str]]:
    db = await get_db()
    async with db.execute(
        'SELECT user_message, bot_response FROM bot_interactions '
        'WHERE username = ? AND user_message NOT LIKE \'[%]\' ORDER BY id DESC LIMIT ?',
        (username, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))


async def get_tagged_answers(tags: list[str], limit: int) -> list[str]:
    """The bot's last answers saved under any of these tags, whoever asked – «[who] nick»
    finds what the bot already said about the nick."""
    db = await get_db()
    marks = ','.join('?' * len(tags))
    async with db.execute(
        f'SELECT bot_response FROM bot_interactions WHERE user_message IN ({marks})'
        ' ORDER BY id DESC LIMIT ?',
        (*tags, limit),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


# Addressings of the bot are counted from the chat messages themselves (the addressed
# column), not from the answers in bot_interactions: a person counts a reply, a call
# by word and a mention as addressing – even if the bot stayed silent (empty answer,
# stop-list, cooldown). A call that carried a command («сосурити !roll») is not
# counted: command messages are not stored at all since 2026-09-18.
# Besides, bot_interactions also holds what the bot said on its own.


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

    Sessions come in two kinds, and adding them into one number is unfair: before the
    switch to stream sessions a session was a calendar day (`2026-09-16`), now it is a
    stream (`2026-09-16 19:00`). They are told apart by the id length.
    """
    db = await get_db()
    async with db.execute('SELECT COUNT(*) FROM chat_messages') as cursor:
        msgs = (await cursor.fetchone())[0]
    async with db.execute('SELECT COALESCE(SUM(addressed), 0) FROM chat_messages') as cursor:
        interactions = (await cursor.fetchone())[0]
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



def _sanitize_fts_query(text: str) -> str:
    cleaned = ''.join(c if c.isalnum() or c == ' ' else ' ' for c in text)
    words = [w + '*' if len(w) > 3 else w for w in cleaned.split()]
    return ' OR '.join(words)


async def search_context(query: str, limit: int = 10) -> list[str]:
    """FTS search over lore and chat history, merged into one list.

    Each source is queried for the full limit: if one yields less than
    its share, the free slots go to the other instead of being lost.
    """
    safe_query = _sanitize_fts_query(query)
    if not safe_query or limit <= 0:
        return []

    db = await get_db()
    knowledge_rows: list[str] = []
    chat_rows: list[str] = []

    try:
        async with db.execute(
            'SELECT content FROM knowledge_fts WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?',
            (safe_query, limit),
        ) as cursor:
            knowledge_rows = [content for (content,) in await cursor.fetchall()]
    except Exception:
        logger.warning('FTS search failed on knowledge_fts', exc_info=True)

    try:
        async with db.execute(
            'SELECT username, message FROM chat_fts WHERE chat_fts MATCH ? ORDER BY rank LIMIT ?',
            (safe_query, limit),
        ) as cursor:
            chat_rows = [f'{username}: {message}' for username, message in await cursor.fetchall()]
    except Exception:
        logger.warning('FTS search failed on chat_fts', exc_info=True)

    knowledge_share = max(1, round(limit * Context.SEARCH_KNOWLEDGE_SHARE)) if knowledge_rows else 0
    results = knowledge_rows[:knowledge_share]
    results += chat_rows[:limit - len(results)]
    if len(results) < limit:
        taken = set(results)
        for row in knowledge_rows[knowledge_share:]:
            if len(results) >= limit:
                break
            if row not in taken:
                results.append(row)
    return results[:limit]


def invalidate_knowledge_cache() -> None:
    """Forget the id list (after a lore import or clear in this process)."""
    global _knowledge_ids
    _knowledge_ids = None


async def _knowledge_id_list(db: aiosqlite.Connection) -> list[int]:
    global _knowledge_ids, _knowledge_ids_at
    if _knowledge_ids is None or time.monotonic() - _knowledge_ids_at > KNOWLEDGE_IDS_TTL:
        async with db.execute('SELECT id FROM knowledge') as cursor:
            _knowledge_ids = [row[0] for row in await cursor.fetchall()]
        _knowledge_ids_at = time.monotonic()
    return _knowledge_ids


async def get_random_knowledge(limit: int = 10) -> list[str]:
    """Random sample from the lore, every row equally likely.

    ORDER BY RANDOM() scanned the whole table (at 96k rows – about 19 ms per bot
    answer). A random id with the nearest row after it was fast but not uniform:
    the row after a gap in the ids took the whole gap's chances, and after
    --clear-lore --source removed a big source from the middle one line showed up
    in half the answers (found in review 2026-09-19). So the ids themselves are
    kept (96k ints, a few MB) and sampled directly.
    """
    if limit <= 0:
        return []
    db = await get_db()
    ids = await _knowledge_id_list(db)
    if not ids:
        return []
    picked = random.sample(ids, min(limit, len(ids)))
    marks = ','.join('?' * len(picked))
    async with db.execute(f'SELECT id, content FROM knowledge WHERE id IN ({marks})', picked) as cursor:
        found = dict(await cursor.fetchall())
    # In the sample's random order; a row deleted since the ids were read is skipped
    return [found[i] for i in picked if i in found]


async def backup_db(destination: str) -> str:
    """Consistent DB copy via the backup API (safe while the bot is running)."""
    db = await get_db()
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    target = await aiosqlite.connect(path)
    try:
        await db.backup(target)
    finally:
        await target.close()
    return str(path)


async def vacuum_db() -> None:
    db = await get_db()
    await db.commit()
    await db.execute('VACUUM')
    await db.commit()


async def record_bot_use(username: str, kind: str) -> None:
    """Record a bot request: the hourly quota is counted from this journal."""
    db = await get_db()
    await db.execute('INSERT INTO bot_uses (username, kind) VALUES (?, ?)', (username, kind))
    await db.commit()


async def forget_bot_use(username: str, kind: str) -> None:
    """Remove the last recorded request: the handler rejected the input format.

    The pair of record_bot_use: the dispatcher records the request before calling the
    handler, and a handler that generated nothing gives the quota slot back –
    the same way it gives back the cooldown.
    """
    db = await get_db()
    await db.execute(
        'DELETE FROM bot_uses WHERE id = (SELECT MAX(id) FROM bot_uses WHERE username = ? AND kind = ?)',
        (username, kind),
    )
    await db.commit()


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
