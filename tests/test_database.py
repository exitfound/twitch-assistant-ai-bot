"""Schema, quotas and the chat queries in src/core/db/, on a temporary database."""
import asyncio
import contextlib

import pytest

from src.core import database
from src.core.database import (
    count_bot_uses, count_channel_bot_uses, forget_bot_use,
    get_user_interactions, has_chatted, init_db, record_bot_use, save_bot_interaction,
    save_chat_message, search_context, transaction,
)
from src.core.db import schema
from src.core.db.knowledge import _sanitize_fts_query
from src.local.roll.storage import save_roll


async def _tables(db) -> set[str]:
    async with db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')") as cursor:
        return {name for (name,) in await cursor.fetchall()}


async def test_init_db_is_idempotent(db):
    """init_db() runs on every start and from every CLI command against the live database."""
    before = await _tables(db)
    await save_chat_message('s', 'gop', 'привет')
    await init_db()
    await init_db()
    assert await _tables(db) == before
    async with db.execute('SELECT COUNT(*) FROM chat_messages') as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_schema_has_every_table(db):
    tables = await _tables(db)
    for name in ('chat_messages', 'bot_uses', 'bot_interactions', 'facts', 'chronicles',
                 'chatter_events', 'memory_state', 'chatter_profiles', 'knowledge', 'rolls',
                 'rewards', 'roll_actions', 'streams', 'roll_perks', 'chat_fts', 'knowledge_fts',
                 'idx_bot_uses_user_time'):
        assert name in tables


def test_fts_query_is_sanitized():
    assert _sanitize_fts_query('"; DROP TABLE x --') == 'DROP* OR TABLE* OR x'
    assert _sanitize_fts_query('!!!') == ''


async def test_search_finds_chat_and_survives_fts_syntax(db):
    await save_chat_message('s', 'gop', 'обсуждаем терраформ и кубернетес')
    assert await search_context('терраформ') == ['gop: обсуждаем терраформ и кубернетес']
    assert await search_context('" OR NEAR(') == []


async def test_has_chatted_looks_at_three_tables(db):
    assert not await has_chatted('a')
    await save_chat_message('s', 'a', 'привет')
    await save_roll('s', 'b', 50, free_throw=True)
    await record_bot_use('c', 'gemini')
    assert all([await has_chatted('a'), await has_chatted('b'), await has_chatted('c')])


async def test_quota_counts_and_refunds(db):
    for _ in range(3):
        await record_bot_use('gop', 'gemini')
    await record_bot_use('gop', 'who')
    await record_bot_use('other', 'gemini')
    assert await count_bot_uses('gop', 'gemini', 60) == 3
    await forget_bot_use('gop', 'gemini')
    assert await count_bot_uses('gop', 'gemini', 60) == 2


async def test_channel_ceiling_counts_only_the_dispatcher_kind(db):
    """!who writes a second row under its own kind: counting it would charge the command twice."""
    await record_bot_use('a', 'gemini')
    await record_bot_use('a', 'who')
    await record_bot_use('b', 'gemini')
    assert await count_channel_bot_uses('gemini', 60) == 2


async def test_user_interactions_skip_every_tagged_row(db):
    """A tag is a prefix: «[who] ник» must be filtered too, or !who reads its own answers."""
    await save_bot_interaction('s', 'gop', 'как дела', 'норм')
    await save_bot_interaction('s', 'gop', '[who] nick', 'досье')
    await save_bot_interaction('s', 'gop', '[ask] вопрос', 'ответ')
    await save_bot_interaction('s', 'gop', '[summary]', 'сводка')
    assert await get_user_interactions('gop') == [('как дела', 'норм')]


async def test_addressed_flag_is_stored(db):
    await save_chat_message('s', 'gop', 'сосурян привет', addressed=True)
    async with (await database.get_db()).execute('SELECT addressed FROM chat_messages') as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_new_database_logs_no_migration(db, caplog):
    """A table created for the first time is not a migration; the warning is kept for
    rebuilding an old-format one."""
    warnings = [r.getMessage() for r in caplog.get_records('setup') if r.levelname == 'WARNING']
    assert not [w for w in warnings if 'Migrated' in w]


async def test_connection_waits_out_a_cli_writer(db):
    """The CLI (--vacuum, --upload-lore) writes to the same file: with SQLite's default
    5 s the bot's chat inserts failed with «database is locked» and were lost."""
    async with db.execute('PRAGMA busy_timeout') as cursor:
        assert (await cursor.fetchone())[0] >= 30_000


async def test_schema_version_is_recorded(db):
    async with db.execute('PRAGMA user_version') as cursor:
        assert (await cursor.fetchone())[0] == schema.SCHEMA_VERSION == len(schema.STEPS)


async def test_a_failing_step_names_itself(db, monkeypatch, caplog):
    """A migration that breaks on the live database must say which one it was."""
    async def broken(db):
        raise RuntimeError('disk I/O error')
    monkeypatch.setattr(schema, 'STEPS', [*schema.STEPS[:2], ('rolls', broken)])
    with pytest.raises(RuntimeError):
        await init_db()
    assert 'rolls' in caplog.text


async def _messages(db) -> list[str]:
    async with db.execute('SELECT message FROM chat_messages ORDER BY id') as cursor:
        return [m for (m,) in await cursor.fetchall()]


async def test_a_failed_transaction_leaves_nothing(db):
    with pytest.raises(RuntimeError):
        async with transaction() as tx:
            await tx.execute("INSERT INTO chat_messages (session_id, username, message) VALUES ('s', 'a', 'x')")
            raise RuntimeError('halfway')
    assert await _messages(db) == []


async def test_a_nested_write_joins_the_outer_transaction(db):
    """save_chat_message() inside a transaction must not commit the outer half on its own."""
    with pytest.raises(RuntimeError):
        async with transaction():
            await save_chat_message('s', 'a', 'inner')
            raise RuntimeError('halfway')
    assert await _messages(db) == []


async def test_another_writer_does_not_commit_a_half_done_transaction(db):
    """On one shared connection a commit() from anyone committed everything pending: a chat
    message saved in the middle of a multi-step write made its first half permanent."""
    halfway = asyncio.Event()

    async def multi_step():
        async with transaction() as tx:
            await tx.execute("INSERT INTO chat_messages (session_id, username, message) VALUES ('s', 'a', 'half')")
            halfway.set()
            await asyncio.sleep(0.05)
            raise RuntimeError('second step failed')

    task = asyncio.create_task(multi_step())
    await halfway.wait()
    await save_chat_message('s', 'b', 'chat')
    with pytest.raises(RuntimeError):
        await task
    assert await _messages(db) == ['chat']


async def test_a_failed_commit_does_not_block_every_later_write(db, monkeypatch):
    """A commit that raises (a full or broken volume) left the transaction open, and every
    later BEGIN failed: the bot went deaf while the heartbeat kept it «healthy»."""
    real_commit = db.commit
    calls = []

    async def failing_once():
        calls.append(1)
        if len(calls) == 1:
            raise OSError('disk full')
        await real_commit()
    monkeypatch.setattr(db, 'commit', failing_once)
    with pytest.raises(OSError):
        await save_chat_message('s', 'a', 'lost')
    await save_chat_message('s', 'b', 'next')
    assert await _messages(db) == ['next']


async def test_a_cancelled_begin_does_not_block_every_later_write(db):
    """aiosqlite still runs a queued BEGIN after its awaiting task is cancelled: nobody
    rolled it back, and the next transaction could not begin."""
    async def write():
        async with transaction() as tx:
            await tx.execute("INSERT INTO chat_messages (session_id, username, message) VALUES ('s', 'a', 'x')")

    task = asyncio.create_task(write())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await save_chat_message('s', 'b', 'next')
    assert await _messages(db) == ['next']


async def _chat(session_id: str, count: int) -> None:
    for i in range(count):
        await database.save_chat_message(session_id, 'gop', f'сообщение {i}')


async def test_previous_session_survives_new_chat(db):
    await _chat('2026-09-18 20:00', 3)
    await _chat('2026-09-19 20:00', 1)
    assert await database.get_previous_chat_session('2026-09-19 20:00', 2) == '2026-09-18 20:00'
    await _chat('2026-09-19 20:00', 5)
    assert await database.get_previous_chat_session('2026-09-19 20:00', 2) == '2026-09-18 20:00'


async def test_previous_session_of_a_silent_stream_is_looked_up_again(db):
    await _chat('2026-09-18 20:00', 3)
    assert await database.get_previous_chat_session('2026-09-19 20:00', 2) is None
    await _chat('2026-09-19 20:00', 1)
    assert await database.get_previous_chat_session('2026-09-19 20:00', 2) == '2026-09-18 20:00'


async def test_last_session_follows_a_new_stream(db):
    """Offline the day session stays the same while a stream in between gets its chat."""
    await _chat('2026-09-18 20:00', 3)
    assert await database.get_last_chat_session('2026-09-22', 2) == '2026-09-18 20:00'
    await _chat('2026-09-22', 5)
    assert await database.get_last_chat_session('2026-09-22', 2) == '2026-09-18 20:00'
    await _chat('2026-09-21 20:00', 1)
    assert await database.get_last_chat_session('2026-09-22', 2) == '2026-09-18 20:00'
    await _chat('2026-09-21 20:00', 1)
    assert await database.get_last_chat_session('2026-09-22', 2) == '2026-09-21 20:00'


async def test_total_stats(db):
    await _chat('2026-09-18 20:00', 2)
    await _chat('2026-09-19', 1)
    await database.save_chat_message('2026-09-19', 'gop', 'сосурян привет', addressed=True)
    assert await database.get_total_stats() == (4, 1, 1, 1)
