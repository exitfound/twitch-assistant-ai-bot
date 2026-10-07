"""bot_state: small settings kept across restarts, key → value."""
from src.core.db.connection import get_db, transaction


async def get_state(key: str) -> str | None:
    db = await get_db()
    async with db.execute('SELECT value FROM bot_state WHERE key = ?', (key,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    async with transaction() as db:
        await db.execute(
            'INSERT INTO bot_state (key, value) VALUES (?, ?)'
            ' ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
            (key, value),
        )
