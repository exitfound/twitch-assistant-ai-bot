import asyncio
import random
import time
import aiosqlite
import logging
from pathlib import Path
from typing import NamedTuple

from src.core.config import Context

logger = logging.getLogger(__name__)

# Корень проекта: src/core/database.py → на три уровня вверх
DB_PATH = Path(__file__).resolve().parents[2] / 'chat_history.db'

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
_knowledge_max_id: int | None = None


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
    # Таблицы игры !roll: rolls, rewards, roll_actions, roll_perks. Схема и миграции здесь,
    # вместе со всеми, запросы к ним — в src/local/roll/storage.py
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
            UNIQUE(session_id, username)
        )
    ''')
    await _migrate_rolls(db)
    # Какие награды за баллы канала бот уже создал в Twitch: id нужен, чтобы
    # при переименовании награды не плодить дубли
    await db.execute('''
        CREATE TABLE IF NOT EXISTS rewards (
            action    TEXT PRIMARY KEY,
            reward_id TEXT NOT NULL
        )
    ''')
    # Журнал купленных наград: кто, кого, чем, что было и что стало. Он же
    # защищает от повторной обработки и хранит щиты — щит это успешная запись
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
    # Бонусы игры по итогам прошлого эфира: щит китежанину, проклятие залупе.
    # active_from — первое появление в эфире, от него идёт отсчёт
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
    # Эфиры канала. Сессия бота — это эфир, а не календарный день: id эфира
    # привязан к сессии, поэтому перезапуск посреди стрима продолжает её же.
    # Короткий обрыв даёт новый id, но ту же сессию
    await db.execute('''
        CREATE TABLE IF NOT EXISTS streams (
            stream_id  TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at   REAL
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
    # Живые БД, созданные до появления UNIQUE(username, fact), опираются на этот
    # индекс — создаём его всегда, чтобы гарантия дедупа не зависела от возраста БД.
    await db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_username_fact ON facts(username, fact)'
    )
    await _drop_legacy_objects(db)
    await _migrate_fts(db)
    await _create_fts_triggers(db)
    await db.commit()


_ROLLS_COLUMNS = {
    # Счётчик бесплатных бросков. Старые строки получают 0: прошлые сессии
    # закрыты, а в текущей каждый начинает с полного запаса
    'free_throws': 'INTEGER NOT NULL DEFAULT 0',
    # Проклятие: потолок ближайшего броска (NULL — не проклят) и момент
    # по time.time(), когда потолок встал на дно, — от него считается снятие
    'curse_ceiling': 'INTEGER',
    'curse_floor_at': 'REAL',
    # Жёсткий срок проклятия по time.time(): у проклятия залупы прошлого эфира
    # он есть, у купленного — NULL
    'curse_until': 'REAL',
}


async def _migrate_rolls(db: aiosqlite.Connection) -> None:
    """Колонки, появившиеся в rolls позже самой таблицы."""
    async with db.execute('PRAGMA table_info(rolls)') as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    for name, ddl in _ROLLS_COLUMNS.items():
        if name not in columns:
            await db.execute(f'ALTER TABLE rolls ADD COLUMN {name} {ddl}')
            logger.warning('В rolls добавлена колонка %s', name)


# Остатки прежней версии: индекс по bot_interactions больше не читается,
# но триггеры продолжали писать в него при каждом взаимодействии.
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


async def save_chat_message(session_id: str, username: str, message: str) -> None:
    db = await get_db()
    await db.execute(
        'INSERT INTO chat_messages (session_id, username, message) VALUES (?, ?, ?)',
        (session_id, username, message),
    )
    await db.commit()


async def save_bot_interaction(session_id: str, username: str, user_message: str, bot_response: str) -> None:
    db = await get_db()
    await db.execute(
        'INSERT INTO bot_interactions (session_id, username, user_message, bot_response) VALUES (?, ?, ?, ?)',
        (session_id, username, user_message, bot_response),
    )
    await db.commit()


async def save_fact(username: str, fact: str) -> None:
    db = await get_db()
    await db.execute(
        'INSERT OR IGNORE INTO facts (username, fact) VALUES (?, ?)',
        (username, fact),
    )
    await db.commit()


def _contains_ci(haystack: str, needle: str) -> bool:
    """Поиск подстроки без учёта регистра.

    SQLite LIKE складывает регистр только для ASCII, поэтому кириллица
    сравнивается на стороне Python: таблица фактов маленькая.
    """
    return needle.casefold() in haystack.casefold()


async def delete_fact(username: str, query: str) -> str | list[str] | None:
    """Удалить факт по подстроке.

    Возвращает:
        str — текст удалённого факта (одно совпадение)
        list[str] — несколько совпадений (нужно уточнить запрос)
        None — не найдено
    """
    db = await get_db()
    async with db.execute(
        'SELECT id, fact FROM facts WHERE username = ? ORDER BY id', (username,)
    ) as cursor:
        rows = await cursor.fetchall()
    matches = [(fact_id, fact) for fact_id, fact in rows if _contains_ci(fact, query)]
    if not matches:
        return None
    if len(matches) > 1:
        return [fact for _, fact in matches]
    await db.execute('DELETE FROM facts WHERE id = ?', (matches[0][0],))
    await db.commit()
    return matches[0][1]


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


async def get_recent_chat(session_id: str, limit: int = 20) -> list[tuple[str, str]]:
    db = await get_db()
    async with db.execute(
        'SELECT username, message FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?',
        (session_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))



async def get_user_messages(username: str, limit: int = 30) -> list[str]:
    db = await get_db()
    async with db.execute(
        'SELECT message FROM chat_messages WHERE username = ? ORDER BY id DESC LIMIT ?',
        (username, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in reversed(rows)]


async def has_chatted(username: str) -> bool:
    """Писал ли ник в чате канала хоть раз, за всё время."""
    db = await get_db()
    async with db.execute(
        'SELECT 1 FROM chat_messages WHERE username = ? LIMIT 1', (username,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def get_user_interactions(username: str, limit: int = 10) -> list[tuple[str, str]]:
    db = await get_db()
    async with db.execute(
        'SELECT user_message, bot_response FROM bot_interactions '
        'WHERE username = ? AND user_message NOT LIKE \'[%]\' ORDER BY id DESC LIMIT ?',
        (username, limit),
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



def _sanitize_fts_query(text: str) -> str:
    cleaned = ''.join(c if c.isalnum() or c == ' ' else ' ' for c in text)
    words = [w + '*' if len(w) > 3 else w for w in cleaned.split()]
    return ' OR '.join(words)


async def search_context(query: str, limit: int = 10) -> list[str]:
    """FTS-поиск по лору и истории чата, объединённый в один список.

    Каждый источник запрашивается на полный лимит: если один даёт меньше
    своей доли, свободные слоты забирает второй, а не теряются.
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
    """Сбросить кэш максимального id (после импорта или очистки лора)."""
    global _knowledge_max_id
    _knowledge_max_id = None


async def _knowledge_max(db: aiosqlite.Connection) -> int:
    global _knowledge_max_id
    if _knowledge_max_id is None:
        async with db.execute('SELECT COALESCE(MAX(id), 0) FROM knowledge') as cursor:
            _knowledge_max_id = (await cursor.fetchone())[0]
    return _knowledge_max_id


async def get_random_knowledge(limit: int = 10) -> list[str]:
    """Случайная выборка из лора.

    ORDER BY RANDOM() сканировал всю таблицу (при 96k записей — около 19 мс
    на каждый ответ бота). Вместо этого берём случайные id и добираем
    ближайшую запись: время не зависит от размера таблицы.
    """
    if limit <= 0:
        return []
    db = await get_db()
    max_id = await _knowledge_max(db)
    if max_id <= 0:
        return []

    results: list[str] = []
    seen: set[int] = set()
    for _ in range(limit * 4):
        if len(results) >= limit:
            break
        target = random.randint(1, max_id)
        async with db.execute(
            'SELECT id, content FROM knowledge WHERE id >= ? ORDER BY id LIMIT 1', (target,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            async with db.execute(
                'SELECT id, content FROM knowledge WHERE id < ? ORDER BY id DESC LIMIT 1', (target,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            break
        if row[0] in seen:
            continue
        seen.add(row[0])
        results.append(row[1])
    return results


async def backup_db(destination: str) -> str:
    """Согласованная копия БД через backup API (безопасно при работающем боте)."""
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
    """time.time() последнего сообщения чата в сессии. None — сообщений не было."""
    db = await get_db()
    async with db.execute(
        "SELECT CAST(strftime('%s', MAX(created_at)) AS REAL) FROM chat_messages WHERE session_id = ?",
        (session_id,),
    ) as cursor:
        (value,) = await cursor.fetchone()
    return value


async def reopen_stream(stream_id: str) -> None:
    """Эфир снова идёт: его закрыли по ошибке — пропущенное событие или задержка Twitch."""
    db = await get_db()
    await db.execute('UPDATE streams SET ended_at = NULL WHERE stream_id = ?', (stream_id,))
    await db.commit()


async def get_session_start(session_id: str) -> float | None:
    """Начало сессии-эфира: старт её первого эфира. None — сессия не эфир, а дата."""
    db = await get_db()
    async with db.execute('SELECT MIN(started_at) FROM streams WHERE session_id = ?', (session_id,)) as cursor:
        (value,) = await cursor.fetchone()
    return value


async def get_previous_stream_session(session_id: str) -> str | None:
    """Сессия эфира, шедшего перед этой. None — более ранних эфиров не записано."""
    start = await get_session_start(session_id)
    db = await get_db()
    async with db.execute(
        'SELECT session_id FROM streams WHERE session_id != ? AND started_at < ?'
        ' ORDER BY started_at DESC LIMIT 1',
        (session_id, start if start is not None else time.time()),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None
