"""Long-term memory without Gemini: conversations split by silence, coverage, and the
state a conversation is left in after each kind of Gemini failure."""
import json

import pytest

from src.core.config import Memory
from src.core.database import get_db
from src.gemini.client import BLOCK_INPUT, ERROR
from src.gemini.memory import build, storage
from src.gemini.memory.storage import Block


async def _say(username: str, message: str, hours_ago: float) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO chat_messages (session_id, username, message, created_at)"
        " VALUES ('s', ?, ?, datetime('now', ?))",
        (username, message, f'-{int(hours_ago * 3600)} seconds'),
    )
    await db.commit()
    return cursor.lastrowid


async def _chronicle_statuses() -> dict[str, str]:
    async with (await get_db()).execute('SELECT conversation, status FROM chronicles') as cursor:
        return dict(await cursor.fetchall())


async def test_chat_is_split_by_silence(db):
    for minute in range(3):
        await _say('a', f'старый разговор {minute}', 10 - minute / 60)
    await _say('a', '!roll', 9.9)
    await _say('b', 'новый разговор', 5)
    await _say('b', 'ещё', 4.95)
    await _say('c', 'идёт прямо сейчас', 0.01)
    blocks = await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False)
    # Commands are not chat; the conversation still going on is not finished
    assert [b.count for b in blocks] == [3, 2]


async def test_covered_messages_are_not_taken_again(db):
    for minute in range(3):
        await _say('a', f'старый {minute}', 10 - minute / 60)
    await _say('b', 'новый', 5)
    first, second = await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False)
    await storage.save_chronicle(first, 'хроника', storage.STATUS_OK, [('a', 'событие')])
    assert await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=True) == [second]


async def test_short_conversation_is_skipped_without_gemini(db):
    block = Block('2026-09-22 20:00', 1, 10, Memory.CONVERSATION_MIN_MESSAGES - 1)
    assert await build.process_block(block)
    assert await _chronicle_statuses() == {block.key: storage.STATUS_SKIPPED}


async def test_unavailable_gemini_leaves_the_conversation_for_later(db, monkeypatch):
    async def down(block):
        raise build.Unavailable
    monkeypatch.setattr(build, 'write_chronicle', down)
    block = Block('2026-09-22 20:00', 1, 100, Memory.CONVERSATION_MIN_MESSAGES)
    assert not await build.process_block(block)
    assert await _chronicle_statuses() == {}


async def test_no_chronicle_is_recorded_as_failed(db, monkeypatch):
    async def nothing(block):
        return None
    monkeypatch.setattr(build, 'write_chronicle', nothing)
    block = Block('2026-09-22 20:00', 1, 100, Memory.CONVERSATION_MIN_MESSAGES)
    assert await build.process_block(block)
    assert await _chronicle_statuses() == {block.key: storage.STATUS_FAILED}


async def test_blocked_chronicle_is_split_in_halves(db, monkeypatch):
    answers = [
        (None, BLOCK_INPUT),
        (json.dumps({'summary': 'первая половина', 'events': [{'nick': 'a', 'event': 'пошутил'}]}), None),
        (json.dumps({'summary': 'вторая половина', 'events': []}), None),
    ]

    async def fake(prompt, config):
        return answers.pop(0)
    monkeypatch.setattr(build, 'generate_checked', fake)
    chat = [('a', f'сообщение {i}') for i in range(2 * build.CHRONICLE_MIN_PIECE)]
    texts, events = await build._chronicle_pieces('key', chat, '–')
    assert texts == ['первая половина', 'вторая половина']
    assert [(e.nick, e.event) for e in events] == [('a', 'пошутил')]


async def test_small_blocked_chronicle_is_not_split(db, monkeypatch):
    async def blocked(prompt, config):
        return None, BLOCK_INPUT
    monkeypatch.setattr(build, 'generate_checked', blocked)
    chat = [('a', 'x')] * (2 * build.CHRONICLE_MIN_PIECE - 1)
    assert await build._chronicle_pieces('key', chat, '–') is None


async def test_unparseable_answer_is_asked_once_more(db, monkeypatch):
    answers = [('{"summary": "обрыв', None), (json.dumps({'summary': 'ок', 'events': []}), None)]

    async def fake(prompt, config):
        return answers.pop(0)
    monkeypatch.setattr(build, 'generate_checked', fake)
    result = await build._ask('prompt', build._Chronicle, 100)
    assert result.summary == 'ок'


@pytest.mark.parametrize('block', [(None, None), ('', None)])
async def test_no_answer_at_all_is_unavailable(db, monkeypatch, block):
    async def fake(prompt, config):
        return block
    monkeypatch.setattr(build, 'generate_checked', fake)
    with pytest.raises(build.Unavailable):
        await build._ask('prompt', build._Chronicle, 100)


async def test_permanent_api_error_is_a_failure_not_an_outage(db, monkeypatch):
    """An outage leaves the conversation for the next check; a 4xx repeats for the same
    prompt, so treating it as an outage would replay the conversation every 10 minutes
    and hold up every later one."""
    async def rejected(prompt, config):
        return None, ERROR
    monkeypatch.setattr(build, 'generate_checked', rejected)
    assert await build._ask('prompt', build._Chronicle, 100) is None


async def test_merge_timeout_keeps_the_paid_pieces(db, monkeypatch):
    """The merge is optional: without it the pieces are joined, and a timeout on it must
    not throw away every piece already paid for and redo them in ten minutes."""
    async def timeout(prompt, config):
        return None, None
    monkeypatch.setattr(build, 'generate_checked', timeout)
    assert await build._merge('key', ['первая', 'вторая']) == 'первая\n\nвторая'


async def test_outage_in_one_half_is_still_an_outage(db, monkeypatch):
    """The halves run as a group: an outage in one cancels the other and surfaces as the
    same Unavailable the callers catch, not as an ExceptionGroup nobody expects."""
    calls = []

    async def fake(prompt, config):
        calls.append(prompt)
        if len(calls) == 1:
            return None, BLOCK_INPUT
        return None, None
    monkeypatch.setattr(build, 'generate_checked', fake)
    chat = [('a', 'x')] * (2 * build.CHRONICLE_MIN_PIECE)
    with pytest.raises(build.Unavailable):
        await build._chronicle_pieces('key', chat, '–')


async def test_malformed_relations_are_dropped_on_read(db):
    """Three readers index relations as r["nick"] / r["note"]: one bad entry in a profile
    turned every free-text answer mentioning that chatter into an error."""
    good = {'nick': 'a', 'note': 'дружат'}
    await storage.save_profile(storage.Profile('gop', 'портрет', [good, {'nick': 'x'}, 'junk', None], 'k', 1))
    assert (await storage.get_profile('gop')).relations == [good]
