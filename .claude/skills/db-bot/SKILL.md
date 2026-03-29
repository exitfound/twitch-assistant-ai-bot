---
name: db-bot
description: Изменения в БД Twitch-бота — новые таблицы, индексы, FTS, миграции, запросы.
disable-model-invocation: true
---

Помоги с изменениями в базе данных бота (`chat_history.db`, SQLite через aiosqlite).

## Архитектура БД

**Соединение:**
- Одно shared-соединение: `get_db()` → `_db` (module-level, защищено `asyncio.Lock`)
- WAL mode (`PRAGMA journal_mode=WAL`)
- Инициализация: `init_db()` в `setup_hook()` — CREATE TABLE IF NOT EXISTS + индексы + FTS + триггеры

**Таблицы:**
- `chat_messages` (id, session_id, username, message, created_at) — все сообщения чата
- `bot_interactions` (id, session_id, username, user_message, bot_response, created_at) — Q&A бота
- `facts` (id, username, fact, created_at) — UNIQUE(username, fact), INSERT OR IGNORE
- `knowledge` (id, content, created_at) — UNIQUE INDEX on content

**FTS5 (content-linked):**
- `chat_fts` → `chat_messages` (username, message) — `content=chat_messages, content_rowid=id`
- `knowledge_fts` → `knowledge` (content) — `content=knowledge, content_rowid=id`
- Синхронизация через триггеры (AFTER INSERT/DELETE/UPDATE) — НЕ вставлять в FTS вручную
- Миграция: `_migrate_fts()` автоматически пересоздаёт FTS если не content-linked

**Индексы:**
- `idx_facts_username_fact` — UNIQUE на (username, fact)
- `idx_knowledge_content` — UNIQUE на content
- `idx_chat_messages_session` — на session_id
- `idx_bot_interactions_session` — на session_id

## Чеклист для изменений

### Новая таблица
1. `CREATE TABLE IF NOT EXISTS` в `init_db()` (src/database.py)
2. Индексы — `CREATE INDEX IF NOT EXISTS` там же
3. Если нужен FTS: добавить в `_FTS_TABLES` — триггеры создадутся автоматически
4. CRUD-функции в `src/database.py`: `async def`, получают `db = await get_db()`
5. Обязательно `await db.commit()` после INSERT/UPDATE/DELETE

### Новый запрос
```python
async def get_something(param: str, limit: int = 10) -> list[tuple[str, str]]:
    db = await get_db()
    async with db.execute(
        'SELECT col1, col2 FROM table WHERE col = ? ORDER BY id DESC LIMIT ?',
        (param, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(rows)  # или reversed(rows) если нужен хронологический порядок
```

### LIKE-запросы
Всегда через `_escape_like()`:
```python
from src.database import _escape_like
f'%{_escape_like(query)}%'
# + ESCAPE '\\' в SQL
```

### FTS-запросы
Через `_sanitize_fts_query()` — экранирует спецсимволы, добавляет prefix matching:
```python
safe = _sanitize_fts_query(query)
db.execute('SELECT ... FROM fts_table WHERE fts_table MATCH ? ORDER BY rank LIMIT ?', (safe, limit))
```

## Ограничения

- `chat_history.db` не в git (.gitignore) — миграции должны быть идемпотентными (`IF NOT EXISTS`)
- session_id = `YYYY-MM-DD` (дата), не UUID
- FTS5 `tokenize='unicode61'` — поддержка кириллицы из коробки
- Нет ORM — сырой SQL через aiosqlite
