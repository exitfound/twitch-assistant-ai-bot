"""The one shared SQLite connection: open, close, backup, vacuum.

Every query in the bot goes through get_db(), so all of them serialize on this
connection: asyncio.gather over queries gives ordering, not parallelism.
"""
import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextvars import ContextVar
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
# Writers take turns: on one shared connection a commit() from anyone would commit
# another writer's half-done work along with its own
_write_lock = asyncio.Lock()
# Set while the current task is inside transaction(): a nested one joins it
_in_transaction: ContextVar[bool] = ContextVar('_in_transaction', default=False)


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



@contextlib.asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    """All writes of the block commit together or not at all. Every write goes through it.

    A nested transaction() in the same task joins the outer one: game.redeem() and the
    storage functions it calls are one transaction, committed at the end of redeem().
    init_db() and vacuum_db() stay outside: they run before anything else writes.
    """
    db = await get_db()
    if _in_transaction.get():
        yield db
        return
    async with _write_lock:
        token = _in_transaction.set(True)
        try:
            await db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            await db.commit()
        finally:
            _in_transaction.reset(token)


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
