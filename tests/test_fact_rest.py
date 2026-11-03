"""Dossier facts the bot has worn out rest: out of the context for a while."""
import asyncio
from unittest.mock import AsyncMock

from src.core.config import Context
from src.gemini import answer_context, dossier, fact_rest
from src.gemini.answer_context import Question
from src.gemini.memory import storage


def _fact(i: int, text: str = '') -> storage.Fact:
    return storage.Fact(i, 'tease', text or f'факт {i}', '2026-10-01 20:00', 1)


def _clock(monkeypatch, start: float = 1000.0) -> list[float]:
    now = [start]
    monkeypatch.setattr(fact_rest.time, 'monotonic', lambda: now[0])
    return now


async def _settle() -> None:
    await asyncio.gather(*fact_rest._checks)


async def test_a_fact_shown_too_often_rests_until_the_window_passes(monkeypatch):
    monkeypatch.setattr(Context, 'FACT_SHOWS', 3)
    monkeypatch.setattr(Context, 'FACT_USES', 0)
    now = _clock(monkeypatch)
    for _ in range(2):
        fact_rest.answered('ответ', [_fact(1)])
    assert not fact_rest.resting(1)
    fact_rest.answered('ответ', [_fact(1)])
    assert fact_rest.resting(1)
    now[0] += Context.FACT_REST_MINUTES * 60 + 1
    assert not fact_rest.resting(1)
    assert fact_rest._shown == {}


async def test_marks_do_not_pile_up(monkeypatch):
    """A fact nobody asks about again leaves memory with the next answer; a rule switched
    off keeps no marks at all."""
    monkeypatch.setattr(Context, 'FACT_SHOWS', 3)
    monkeypatch.setattr(Context, 'FACT_USES', 0)
    now = _clock(monkeypatch)
    fact_rest.answered('ответ', [_fact(1)])
    now[0] += Context.FACT_REST_MINUTES * 60 + 1
    fact_rest.answered('ответ', [_fact(2)])
    assert set(fact_rest._shown) == {2}
    monkeypatch.setattr(Context, 'FACT_SHOWS', 0)
    fact_rest.answered('ответ', [_fact(3)])
    assert set(fact_rest._shown) == {2}


async def test_a_fact_used_twice_rests(monkeypatch):
    """The check names facts by number, retold ones too; a fact shown but not used keeps
    its place."""
    monkeypatch.setattr(Context, 'FACT_SHOWS', 0)
    monkeypatch.setattr(Context, 'FACT_USES', 2)
    _clock(monkeypatch)
    generate = AsyncMock(return_value='2')
    monkeypatch.setattr(fact_rest, 'generate', generate)
    facts = [_fact(1, 'кофе'), _fact(2, 'разбил макбук')]
    for _ in range(2):
        fact_rest.answered('экран макбуку расколотил', facts)
        await _settle()
    assert fact_rest.resting(2) and not fact_rest.resting(1)
    prompt = generate.await_args.args[0]
    assert 'prompts.fact_check' in prompt


async def test_the_check_reads_numbers_and_ignores_strangers(monkeypatch):
    monkeypatch.setattr(fact_rest, 'generate', AsyncMock(return_value='1, 3 и 7'))
    facts = [_fact(10), _fact(20), _fact(30)]
    assert await fact_rest.used('ответ', facts) == [facts[0], facts[2]]
    monkeypatch.setattr(fact_rest, 'generate', AsyncMock(return_value='0'))
    assert await fact_rest.used('ответ', facts) == []
    # A number in a remark after the list is not a fact's
    monkeypatch.setattr(fact_rest, 'generate', AsyncMock(return_value='Номера: 2 (про 3 клавиатуры)'))
    assert await fact_rest.used('ответ', facts) == [facts[1]]
    monkeypatch.setattr(fact_rest, 'generate', AsyncMock(return_value=None))
    assert await fact_rest.used('ответ', facts) == []


async def test_a_failed_check_rests_nothing(monkeypatch):
    monkeypatch.setattr(Context, 'FACT_USES', 1)
    monkeypatch.setattr(fact_rest, 'generate', AsyncMock(side_effect=RuntimeError('gemini')))
    fact_rest.answered('ответ', [_fact(1)])
    await _settle()
    assert fact_rest._used == {}


async def test_no_facts_no_check(monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(fact_rest, 'generate', generate)
    fact_rest.answered('ответ', [])
    assert not fact_rest._checks
    generate.assert_not_awaited()


async def test_the_dossier_leaves_resting_facts_out_and_names_the_shown(db, monkeypatch):
    await storage.apply_dossier(storage.Profile('gop', 'шапка', [], 'k', 1),
                                [('tease', 'разбил макбук'), ('interest', 'кофе')], [], [], 40)
    facts = (await storage.get_facts_of(['gop']))['gop']
    worn = next(f for f in facts if f.text == 'разбил макбук')
    monkeypatch.setattr(fact_rest, 'resting', lambda fact_id: fact_id == worn.id)
    picked = []
    lines = await dossier.people(['gop'], '', 5, 0, picked=picked)
    assert 'кофе' in lines[0] and 'макбук' not in lines[0]
    assert [f.text for f in picked] == ['кофе']


async def test_only_the_rung_that_answered_counts_its_facts(db, monkeypatch):
    """The last rung carries no dossier: an answer from it showed no facts."""
    await storage.apply_dossier(storage.Profile('gop', 'шапка', [], 'k', 1), [('tease', 'разбил макбук')],
                                [], [], 40)
    question = Question(session_id='2026-10-12 20:00', user='gop', prompt='привет')
    for rung, expected in (('всё', ['разбил макбук']), (answer_context.CHAT_ONLY, [])):
        monkeypatch.setattr(answer_context, 'walk', AsyncMock(return_value=('ответ', rung)))
        shown = []
        await answer_context.answer(question, None, shown)
        assert [f.text for f in shown] == expected

