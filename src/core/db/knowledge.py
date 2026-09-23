"""knowledge and facts: FTS search, the random «language» sample, saved facts."""
import logging
import random
import time

import aiosqlite

from src.core.config import Context
from src.core.db.connection import get_db

logger = logging.getLogger(__name__)

# Ids of all knowledge rows for the random «language» sample, and when they were read
_knowledge_ids: list[int] | None = None
_knowledge_ids_at = 0.0
# The CLI imports into the same file while the bot runs, and cannot reset the bot's
# cache: the ids are re-read this often, so new lore reaches the sample on its own
KNOWLEDGE_IDS_TTL = 600


def _contains_ci(haystack: str, needle: str) -> bool:
    """Case-insensitive substring search.

    SQLite LIKE folds case only for ASCII, so Cyrillic is compared
    on the Python side: the facts table is small.
    """
    return needle.casefold() in haystack.casefold()


async def get_all_facts() -> list[tuple[str, str, str]]:
    """Every saved fact as (author, fact, created_at), grouped by author – for --list-facts."""
    db = await get_db()
    async with db.execute('SELECT username, fact, created_at FROM facts ORDER BY username, id') as cursor:
        return await cursor.fetchall()


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


def _sanitize_fts_query(text: str) -> str:
    # Lowercase: an uppercase AND, OR, NOT or NEAR is an FTS5 operator, and the search
    # ignores case anyway
    cleaned = ''.join(c if c.isalnum() or c == ' ' else ' ' for c in text.lower())
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

    The id list is held in memory (96k ints, a few MB) and sampled directly. ORDER BY
    RANDOM() scans the whole table, 19 ms per answer; picking a random id and taking the
    nearest row after it is fast but not uniform – the row after a gap in the ids takes
    the whole gap's chances, and --clear-lore --source leaves exactly such gaps.
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
