"""Schema and migrations of every table, roll ones included: init_db().

It runs on every bot start and from every CLI command against the live database,
while the bot may be running, so every step is idempotent and additive.
"""
import logging

import aiosqlite

from src.core.db.connection import get_db, reopen
from src.core.utils import SOSUR_RE

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Bring the schema up to date, step by step, and open the database for use.

    Each step checks for itself what is already done, because it runs on every start and
    from every CLI command against the live database, possibly while an older bot process
    is running on it. user_version records how many steps the file has been through.
    """
    # The one explicit way to open the database again after close_db()
    reopen()
    db = await get_db()
    async with db.execute('PRAGMA user_version') as cursor:
        (version,) = await cursor.fetchone()
    for name, step in STEPS:
        try:
            await step(db)
        except Exception:
            logger.error('Схема: шаг «%s» не выполнен', name)
            raise
    if version != SCHEMA_VERSION:
        # Written last: only a start that went through every step records the version
        await db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        logger.info('Схема: версия %d → %d', version, SCHEMA_VERSION)
    await db.commit()


async def _chat_messages(db: aiosqlite.Connection) -> None:
    """Chat_messages and its addressed column."""
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


async def _bot_interactions(db: aiosqlite.Connection) -> None:
    """Bot_interactions: what the bot answered."""
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


async def _facts(db: aiosqlite.Connection) -> None:
    """Facts from the retired !fact."""
    await db.execute('''
        CREATE TABLE IF NOT EXISTS facts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            fact       TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, fact)
        )
    ''')


async def _knowledge(db: aiosqlite.Connection) -> None:
    """Knowledge, with the source column and its index."""
    # source – where a row came from (a file, a Telegram chat, 'facts'), so one
    # source can be removed whole with --clear-lore --source. NULL where it is unknown
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


async def _bot_uses(db: aiosqlite.Connection) -> None:
    """Bot_uses: the journal of Gemini requests."""
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


async def _rolls(db: aiosqlite.Connection) -> None:
    """Rolls and its added columns."""
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


async def _rewards(db: aiosqlite.Connection) -> None:
    """Rewards: action → Twitch reward id."""
    # Which channel-points rewards the bot has already created in Twitch: the id is
    # needed so that renaming a reward does not spawn duplicates
    await db.execute('''
        CREATE TABLE IF NOT EXISTS rewards (
            action    TEXT PRIMARY KEY,
            reward_id TEXT NOT NULL
        )
    ''')


async def _roll_actions(db: aiosqlite.Connection) -> None:
    """Roll_actions: the journal of redemptions."""
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


async def _roll_perks(db: aiosqlite.Connection) -> None:
    """Roll_perks: perks from the previous stream."""
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


async def _streams(db: aiosqlite.Connection) -> None:
    """Streams: Twitch stream id → bot session."""
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


async def _memory(db: aiosqlite.Connection) -> None:
    """The memory: chronicles, chatter_events, memory_state, chatter_profiles."""
    # The bot's memory of chatters (src/gemini/memory/), keyed by conversation – chat
    # between two long silences, named by the Moscow time of its first message. A row
    # covers messages first_id..last_id; 'failed' and 'skipped' are not retried
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
    # The memory's own state: 'built' is set once bot.py --build-memory has walked the
    # whole history. Until then the bot leaves the memory alone, because a half-built
    # one would be replayed conversation by conversation at many times the cost
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


async def _indexes(db: aiosqlite.Connection) -> None:
    """Indexes on the older tables."""
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
    # Always created: the dedup guarantee must not depend on whether the facts table
    # itself was built with UNIQUE(username, fact).
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_username_fact ON facts(username, fact)'
    )


async def _legacy(db: aiosqlite.Connection) -> None:
    """Drop retired tables and triggers."""
    await _drop_legacy_objects(db)


async def _fts(db: aiosqlite.Connection) -> None:
    """FTS5 tables and their sync triggers."""
    await _migrate_fts(db)
    await _create_fts_triggers(db)


# In this order: the memory migration renames tables the later steps create anew, and
# the indexes and FTS tables need the tables they are built on
STEPS = [
    ('chat_messages', _chat_messages),
    ('bot_interactions', _bot_interactions),
    ('facts', _facts),
    ('knowledge', _knowledge),
    ('bot_uses', _bot_uses),
    ('rolls', _rolls),
    ('rewards', _rewards),
    ('roll_actions', _roll_actions),
    ('roll_perks', _roll_perks),
    ('streams', _streams),
    ('memory', _memory),
    ('indexes', _indexes),
    ('legacy', _legacy),
    ('fts', _fts),
]
SCHEMA_VERSION = len(STEPS)


_ROLLS_COLUMNS = {
    # Free throw counter. Defaults to 0: past sessions are closed, and in the
    # current one everybody starts with the full allowance
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

    Addressing is a сосур*/secur* word, a mention of the bot's @nick or a reply to its
    message; the dispatcher sets the flag. Existing rows are backfilled from their text
    alone, because past replies and mentions cannot be recovered, so their counts are a
    lower bound.
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
    """Rename the session-keyed memory tables to the conversation-keyed ones.

    stream_chronicles → chronicles, session_id → conversation, updated_session →
    last_conversation; a session-keyed chronicle gets the message range of its
    session so the memory does not take it again. Runs before the CREATE TABLEs,
    which would otherwise make the new tables empty.
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


# Nothing reads the bot_interactions FTS index, while its triggers write to it on
# every interaction, so both the triggers and the table are dropped.
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
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (fts_name,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and 'content=' in row[0]:
            continue

        cols = ', '.join(columns)
        await db.execute(f'DROP TABLE IF EXISTS {fts_name}')
        await db.execute(
            f"CREATE VIRTUAL TABLE {fts_name} USING fts5("
            f"{cols}, content={source}, content_rowid=id, tokenize='unicode61')"
        )
        await db.execute(f"INSERT INTO {fts_name}({fts_name}) VALUES('rebuild')")
        if row is None:
            # A new database: the table is created, nothing was migrated
            logger.info('Создан %s (поиск по %s)', fts_name, source)
        else:
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
