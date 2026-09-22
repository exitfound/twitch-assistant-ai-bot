"""Schema, quotas and the chat queries in src/core/db/, on a temporary database."""
from src.core import database
from src.core.database import (
    count_bot_uses, count_channel_bot_uses, forget_bot_use,
    get_user_interactions, has_chatted, init_db, record_bot_use, save_bot_interaction,
    save_chat_message, search_context,
)
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
