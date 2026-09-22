"""What goes into a free-text answer and !summary: the character budget of the chat."""
from unittest.mock import AsyncMock

from src.core.config import Context, Memory
from src.core.database import get_db, save_chat_message
from src.gemini import answer_context, summary
from src.gemini.answer_context import Question
from src.gemini.context import chat_chars, tail_within

PREV = '2026-09-18 20:00'
NOW = '2026-09-19 20:00'


def test_tail_within_keeps_the_latest_messages():
    pairs = [('a', 'x' * 10), ('b', 'y' * 10), ('c', 'z' * 10)]
    assert chat_chars(pairs) == 3 * 14
    assert tail_within(pairs, 30) == pairs[-2:]
    assert tail_within(pairs, 1000) == pairs
    assert tail_within(pairs, 5) == []


async def _fill(session_id: str, count: int, tag: str) -> None:
    for i in range(count):
        await save_chat_message(session_id, 'gop', f'{tag}{i:03d} ' + 'x' * 40)


async def test_ladder_cuts_the_previous_stream_first(db, monkeypatch):
    monkeypatch.setattr(Memory, 'CONVERSATION_MIN_MESSAGES', 1)
    await _fill(PREV, 60, 'old')
    await _fill(NOW, 60, 'now')
    per_message = chat_chars([('gop', 'now000 ' + 'x' * 40)])
    # Room for the whole current stream and the last ten of the previous one
    monkeypatch.setattr(Context, 'STREAM_MAX_CHARS', per_message * 70)
    rungs = dict(await answer_context.ladder(Question(session_id=NOW, user='gop', prompt='как дела')))
    both = rungs['два стрима']
    assert 'now000' in both and 'now059' in both
    assert 'old050' in both and 'old049' not in both


async def test_ladder_cuts_the_start_of_a_huge_current_stream(db, monkeypatch):
    monkeypatch.setattr(Memory, 'CONVERSATION_MIN_MESSAGES', 1)
    await _fill(PREV, 10, 'old')
    await _fill(NOW, 60, 'now')
    per_message = chat_chars([('gop', 'now000 ' + 'x' * 40)])
    monkeypatch.setattr(Context, 'STREAM_MAX_CHARS', per_message * 20)
    rungs = dict(await answer_context.ladder(Question(session_id=NOW, user='gop', prompt='как дела')))
    first = next(iter(rungs.values()))
    assert 'now059' in first and 'now040' in first and 'now039' not in first
    assert 'old0' not in first


async def test_summary_keeps_to_the_budget(db, monkeypatch):
    await _fill(NOW, 60, 'now')
    per_message = chat_chars([('gop', 'now000 ' + 'x' * 40)])
    monkeypatch.setattr(Context, 'STREAM_MAX_CHARS', per_message * 20)
    walk = AsyncMock(return_value=('ok', 'весь стрим'))
    monkeypatch.setattr(summary, 'walk', walk)
    await summary.now(NOW, 'gop')
    first = walk.await_args.args[0][0][1]
    assert 'now059' in first and 'now040' in first and 'now039' not in first


async def _profile(nick: str) -> None:
    db = await get_db()
    await db.execute(
        'INSERT INTO chatter_profiles (username, portrait, relations, last_conversation, sessions_seen)'
        " VALUES (?, ?, '[]', 'k', 1)", (nick, f'портрет {nick}'))
    await db.commit()


async def test_people_are_the_asker_and_the_logins_named(db):
    for nick in ('gop', 'nosok222', 'dv4mp1r3'):
        await _profile(nick)
    lines = await answer_context._people('gop', 'что скажешь про @Nosok222, dv4mp1r3 и васю')
    assert [line.split(':')[0] for line in lines] == ['gop', 'nosok222', 'dv4mp1r3']


def test_russian_words_are_not_logins():
    assert answer_context._NICK.findall('привет как дела gop5ter @ne2oi iphoneы') == ['gop5ter', 'ne2oi']
