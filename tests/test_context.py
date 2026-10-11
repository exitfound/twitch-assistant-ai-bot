"""What goes into a free-text answer and !summary."""
from unittest.mock import AsyncMock

from src.core.config import Context
from src.core.database import get_db, save_chat_message
from src.gemini import answer_context, dossier, summary
from src.gemini.answer_context import Question
from src.gemini.context import chat_chars, tail_within
from src.gemini.memory import storage

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


async def test_ladder_sends_the_thread_not_the_stream(db, monkeypatch):
    """The raw chat is the last CONTEXT_CHAT_MESSAGES of this stream: the earlier ones
    and the previous stream come retold (pulse, chronicle), not as lines to paste."""
    monkeypatch.setattr(Context, 'CHAT_MESSAGES', 20)
    await _fill(PREV, 10, 'old')
    await _fill(NOW, 60, 'now')
    question = Question(session_id=NOW, user='gop', prompt='как дела', pulse='чат обсуждает варик')
    rungs = dict(await answer_context.ladder(question))
    first = rungs['всё']
    assert 'now059' in first and 'now040' in first and 'now039' not in first
    assert 'old0' not in first
    assert 'чат обсуждает варик' in first
    assert 'чат обсуждает варик' not in rungs['только чат']


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
    await storage.apply_dossier(storage.Profile(nick, f'портрет {nick}', [], 'k', 1, ('коронка',)),
                                [('who', f'факт про {nick}')], [], [], 40)


async def test_people_are_the_asker_and_the_logins_named(db):
    for nick in ('gop', 'nosok222', 'dv4mp1r3'):
        await _profile(nick)
    lines = await answer_context._people('gop', 'что скажешь про @Nosok222, dv4mp1r3 и васю')
    assert [line.split(':')[0] for line in lines] == ['gop', 'nosok222', 'dv4mp1r3']


def test_russian_words_are_not_logins():
    assert answer_context._NICK.findall('привет как дела gop5ter @ne2oi iphoneы') == ['gop5ter', 'ne2oi']


async def test_what_lasts_the_stream_comes_first(db):
    """The pulse and the chat lead, so consecutive answers share a prefix for Gemini's
    implicit cache; what depends on the asker and the question comes after."""
    await _fill(NOW, 3, 'now')
    await _profile('gop')
    db_ = await get_db()
    await db_.execute("INSERT INTO facts (username, fact) VALUES ('gop', 'факт')")
    await db_.commit()
    question = Question(session_id=NOW, user='gop', prompt='как дела', pulse='пульс')
    text = dict(await answer_context.ladder(question))['всё']
    order = [text.index(f'[labels.{label}]') for label in ('pulse', 'chat', 'facts', 'dossier')]
    assert order == sorted(order)
    assert text.rstrip().endswith('prompts.user_question')


def test_dossier_line_takes_two_relations():
    relations = [{'nick': f'n{i}', 'note': 'кореш'} for i in range(8)]
    line = dossier.line('gop', storage.Profile('gop', 'шапка', relations, 'k', 1), [])
    assert line.count('кореш') == dossier.RELATIONS


async def test_words_that_are_not_chatters_take_no_place_among_the_dossiers(db):
    """«кто круче в dota и pubg, ivan или gopnik»: the four places go to people with a dossier."""
    for nick in ('asker', 'ivan', 'gopnik'):
        await storage.apply_dossier(storage.Profile(nick, f'шапка {nick}', [], 'k', 1), [], [], [], 40)
    lines = await answer_context._people('asker', 'кто круче в dota и pubg, ivan или gopnik')
    assert [line.split(':')[0] for line in lines] == ['asker', 'ivan', 'gopnik']


async def test_a_dossier_with_nothing_in_it_gives_no_line(db):
    await storage.save_profile(storage.Profile('gop', '', [], 'k', 1))
    assert await dossier.people(['gop'], '', 3, 1) == []
    assert dossier.line('gop', None, []) == ''
