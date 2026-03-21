"""Работа с базой знаний (knowledge)."""
import logging

from src.database import get_db

logger = logging.getLogger(__name__)


def parse_lore_file(path: str) -> list[str]:
    """Парсинг txt файла с лором. Одна запись на строку, # — комментарии."""
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        entries.append(line)
    return entries


def dedup_entries(entries: list[str]) -> list[str]:
    """Убрать дубликаты, сохраняя порядок."""
    seen = set()
    unique = []
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


async def clear_knowledge():
    """Очистить таблицу knowledge (FTS синхронизируется триггером)."""
    db = await get_db()
    await db.execute('DELETE FROM knowledge')
    await db.commit()


async def import_entries(entries: list[str]) -> tuple[int, int]:
    """Импортировать записи в knowledge (FTS синхронизируется триггером). Возвращает (added, skipped)."""
    db = await get_db()
    added = 0
    skipped = 0
    for entry in entries:
        cursor = await db.execute(
            'INSERT OR IGNORE INTO knowledge (content) VALUES (?)', (entry,)
        )
        if cursor.rowcount:
            added += 1
        else:
            skipped += 1
    await db.commit()
    return added, skipped
