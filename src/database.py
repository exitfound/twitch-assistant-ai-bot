import logging

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = 'chat_history.db'

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        await _db.execute('PRAGMA journal_mode=WAL')
    return _db


async def close_db():
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db():
    db = await get_db()
    await db.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            username   TEXT NOT NULL,
            message    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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
    await db.execute('''
        CREATE TABLE IF NOT EXISTS knowledge (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_username_fact ON facts(username, fact)'
    )
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_content ON knowledge(content)'
    )
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)'
    )
    await db.execute(
        'CREATE INDEX IF NOT EXISTS idx_bot_interactions_session ON bot_interactions(session_id)'
    )
    await _migrate_fts(db)
    await _create_fts_triggers(db)
    await db.commit()


_FTS_TABLES = [
    ('chat_fts', 'chat_messages', ['username', 'message']),
    ('knowledge_fts', 'knowledge', ['content']),
]


async def _migrate_fts(db: aiosqlite.Connection):
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


async def _create_fts_triggers(db: aiosqlite.Connection):
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


async def save_chat_message(session_id: str, username: str, message: str):
    db = await get_db()
    await db.execute(
        'INSERT INTO chat_messages (session_id, username, message) VALUES (?, ?, ?)',
        (session_id, username, message),
    )
    await db.commit()


async def save_bot_interaction(session_id: str, username: str, user_message: str, bot_response: str):
    db = await get_db()
    await db.execute(
        'INSERT INTO bot_interactions (session_id, username, user_message, bot_response) VALUES (?, ?, ?, ?)',
        (session_id, username, user_message, bot_response),
    )
    await db.commit()


async def save_fact(username: str, fact: str):
    db = await get_db()
    await db.execute(
        'INSERT OR IGNORE INTO facts (username, fact) VALUES (?, ?)',
        (username, fact),
    )
    await db.commit()


async def delete_fact(username: str, query: str) -> str | list[str] | None:
    """Delete a fact by substring match.

    Returns:
        str — deleted fact text (single match)
        list[str] — multiple matches (user needs to be more specific)
        None — not found
    """
    db = await get_db()
    async with db.execute(
        'SELECT id, fact FROM facts WHERE username = ? AND fact LIKE ?',
        (username, f'%{query}%'),
    ) as cursor:
        matches = await cursor.fetchall()
    if not matches:
        return None
    if len(matches) > 1:
        return [fact for _, fact in matches]
    await db.execute('DELETE FROM facts WHERE id = ?', (matches[0][0],))
    await db.commit()
    return matches[0][1]


async def get_relevant_facts(username: str, query: str) -> list[tuple]:
    db = await get_db()
    async with db.execute(
        'SELECT username, fact FROM facts WHERE username = ? ORDER BY id',
        (username,),
    ) as cursor:
        user_facts = list(await cursor.fetchall())

    words = [w for w in query.split() if len(w) > 3]
    other_facts = []
    if words:
        placeholders = ' OR '.join('fact LIKE ?' for _ in words)
        params = tuple(f'%{w}%' for w in words) + (username,)
        async with db.execute(
            f'SELECT username, fact FROM facts WHERE ({placeholders}) AND username != ? ORDER BY id',
            params,
        ) as cursor:
            other_facts = list(await cursor.fetchall())

    seen = set(user_facts)
    for row in other_facts:
        if row not in seen:
            seen.add(row)
            user_facts.append(row)
    return user_facts


async def get_recent_chat(session_id: str, limit: int = 20) -> list[tuple]:
    db = await get_db()
    async with db.execute(
        'SELECT username, message FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?',
        (session_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))




async def get_session_stats(session_id: str) -> tuple[int, int]:
    db = await get_db()
    async with db.execute(
        'SELECT COUNT(*) FROM chat_messages WHERE session_id = ?', (session_id,)
    ) as cursor:
        msgs = (await cursor.fetchone())[0]
    async with db.execute(
        'SELECT COUNT(*) FROM bot_interactions WHERE session_id = ?', (session_id,)
    ) as cursor:
        interactions = (await cursor.fetchone())[0]
    return msgs, interactions


async def get_total_stats() -> tuple[int, int, int]:
    db = await get_db()
    async with db.execute('SELECT COUNT(*) FROM chat_messages') as cursor:
        msgs = (await cursor.fetchone())[0]
    async with db.execute('SELECT COUNT(*) FROM bot_interactions') as cursor:
        interactions = (await cursor.fetchone())[0]
    async with db.execute(
        'SELECT COUNT(DISTINCT session_id) FROM chat_messages'
    ) as cursor:
        sessions = (await cursor.fetchone())[0]
    return msgs, interactions, sessions


async def get_session_top_users(session_id: str, limit: int = 5) -> list[tuple[str, int]]:
    db = await get_db()
    async with db.execute(
        'SELECT username, COUNT(*) as cnt FROM bot_interactions '
        'WHERE session_id = ? GROUP BY username ORDER BY cnt DESC LIMIT ?',
        (session_id, limit),
    ) as cursor:
        return [(row[0], row[1]) for row in await cursor.fetchall()]


async def get_total_top_users(limit: int = 5) -> list[tuple[str, int]]:
    db = await get_db()
    async with db.execute(
        'SELECT username, COUNT(*) as cnt FROM bot_interactions '
        'GROUP BY username ORDER BY cnt DESC LIMIT ?',
        (limit,),
    ) as cursor:
        return [(row[0], row[1]) for row in await cursor.fetchall()]


def _sanitize_fts_query(text: str) -> str:
    cleaned = ''.join(c if c.isalnum() or c == ' ' else ' ' for c in text)
    words = []
    for w in cleaned.split():
        if not w:
            continue
        prefix = w + '*' if len(w) > 3 else w
        words.append(prefix)
    return ' OR '.join(words)


async def search_context(query: str, limit: int = 10) -> list[str]:
    """FTS search across knowledge and chat history, combined into one result list."""
    safe_query = _sanitize_fts_query(query)
    if not safe_query:
        return []

    results = []
    db = await get_db()
    half = max(limit // 2, 1)

    try:
        async with db.execute(
            'SELECT content FROM knowledge_fts WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?',
            (safe_query, half),
        ) as cursor:
            for (content,) in await cursor.fetchall():
                results.append(content)
    except Exception:
        logger.warning('FTS search failed on knowledge_fts', exc_info=True)

    try:
        async with db.execute(
            'SELECT username, message FROM chat_fts WHERE chat_fts MATCH ? ORDER BY rank LIMIT ?',
            (safe_query, half),
        ) as cursor:
            for username, message in await cursor.fetchall():
                results.append(f'{username}: {message}')
    except Exception:
        logger.warning('FTS search failed on chat_fts', exc_info=True)

    return results[:limit]


async def get_random_knowledge(limit: int = 10) -> list[str]:
    db = await get_db()
    async with db.execute(
        'SELECT content FROM knowledge ORDER BY RANDOM() LIMIT ?',
        (limit,),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]
