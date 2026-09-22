"""The one shared SQLite connection: open, close, backup, vacuum.

Every query in the bot goes through get_db(), so all of them serialize on this
connection: asyncio.gather over queries gives ordering, not parallelism.
"""
import asyncio
from pathlib import Path

import aiosqlite

# DB_PATH may be moved by BOT_DB_PATH – see src/core/paths.py
from src.core.paths import DB_PATH


_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()
# Set by close_db(): a task finishing after shutdown must fail, not open a new connection
# whose non-daemon aiosqlite thread keeps the process from exiting. init_db() clears it
_closed = False
# Seconds a query waits for another process's write lock before failing
BUSY_TIMEOUT = 30


async def get_db() -> aiosqlite.Connection:
    global _db
    async with _db_lock:
        if _closed:
            raise RuntimeError('База уже закрыта: процесс завершается')
        if _db is None:
            # A CLI command writing to the same file (--vacuum, a big --upload-lore) holds
            # the lock longer than SQLite's default 5 s wait, and a chat insert would fail
            _db = await aiosqlite.connect(DB_PATH, timeout=BUSY_TIMEOUT)
            await _db.execute('PRAGMA journal_mode=WAL')
            await _db.execute('PRAGMA synchronous=NORMAL')
    return _db


async def close_db() -> None:
    global _db, _closed
    async with _db_lock:
        _closed = True
        if _db is not None:
            await _db.close()
            _db = None


def reopen() -> None:
    """Let get_db() open the database again after close_db(). Only init_db() calls it."""
    global _closed
    _closed = False


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
