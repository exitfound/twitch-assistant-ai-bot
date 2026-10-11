"""Long-term memory without Gemini: conversations split by silence, coverage, and the
state a conversation is left in after each kind of Gemini failure."""
import json

import pytest

from src.core.config import Gemini, Memory
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
    await storage.save_chronicle(first, 'хроника', storage.ChronicleStatus.OK, [('a', 'событие')])
    assert await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=True) == [second]


async def test_a_covered_conversation_between_two_uncovered_ones(db):
    for hours_ago in (20, 12, 6):
        await _say('a', f'разговор {hours_ago}', hours_ago)
        await _say('b', f'ответ {hours_ago}', hours_ago - 0.1)
    first, middle, last = await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False)
    await storage.save_chronicle(middle, 'хроника', storage.ChronicleStatus.OK, [])
    assert await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=True) == [first, last]


async def test_short_conversation_is_skipped_without_gemini(db):
    block = Block('2026-09-22 20:00', 1, 10, Memory.CONVERSATION_MIN_MESSAGES - 1)
    assert await build.process_block(block)
    assert await _chronicle_statuses() == {block.key: storage.ChronicleStatus.SKIPPED}


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
    assert await _chronicle_statuses() == {block.key: storage.ChronicleStatus.FAILED}


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


# --- dossier ---------------------------------------------------------------------

def _dossier_answer(**fields) -> str:
    answer = {'headline': 'шапка', 'new_facts': [], 'confirmed': [], 'outdated': [],
              'relations': [], 'catchphrases': []}
    return json.dumps({**answer, **fields}, ensure_ascii=False)


def _gemini(monkeypatch, *answers: str) -> list[str]:
    prompts = []
    queue = list(answers)

    async def fake(prompt, config):
        prompts.append(prompt)
        return queue.pop(0), None
    monkeypatch.setattr(build, 'generate_checked', fake)
    return prompts


async def test_dossier_merges_instead_of_rewriting(db):
    profile = storage.Profile('gop', 'шапка', [], 'k1', 1)
    await storage.apply_dossier(profile, [('who', 'админ'), ('interest', 'варик'), ('tease', 'ноет')], [], [], 40)
    admin, wc3, _ = await storage.get_facts('gop')
    await storage.apply_dossier(profile._replace(last_conversation='k2'), [('who', 'переехал')],
                                [wc3.id], [admin.id], 40)
    facts = {f.text: f for f in await storage.get_facts('gop')}
    assert set(facts) == {'варик', 'ноет', 'переехал'}
    assert facts['варик'].hits == 2 and facts['варик'].last_seen == 'k2'
    assert facts['ноет'].hits == 1 and facts['ноет'].last_seen == 'k1'


async def test_dossier_ignores_another_chatters_ids(db):
    await storage.apply_dossier(storage.Profile('a', '', [], 'k', 1), [('who', 'чужой факт')], [], [], 40)
    (other,) = await storage.get_facts('a')
    await storage.apply_dossier(storage.Profile('b', '', [], 'k', 1), [], [other.id], [other.id], 40)
    assert await storage.get_facts('a') == [other]


async def test_dossier_keeps_the_most_confirmed_over_the_cap(db):
    profile = storage.Profile('gop', '', [], 'k1', 1)
    await storage.apply_dossier(profile, [('who', f'факт {i}') for i in range(3)], [], [], 40)
    first = (await storage.get_facts('gop'))[0]
    await storage.apply_dossier(profile._replace(last_conversation='k2'), [('who', 'новый')], [first.id], [], 2)
    assert [f.text for f in await storage.get_facts('gop')] == ['факт 0', 'новый']


async def test_a_full_dossier_of_confirmed_facts_still_takes_a_new_one(db):
    """A new fact has one hit: over the cap it must push out an old one, not itself."""
    profile = storage.Profile('gop', '', [], 'k1', 1)
    await storage.apply_dossier(profile, [('who', f'факт {i}') for i in range(3)], [], [], 3)
    ids = [f.id for f in await storage.get_facts('gop')]
    await storage.apply_dossier(profile._replace(last_conversation='k2'), [], ids, [], 3)
    await storage.apply_dossier(profile._replace(last_conversation='k3'), [('who', 'новый')], ids[1:], [], 3)
    assert [f.text for f in await storage.get_facts('gop')] == ['факт 1', 'факт 2', 'новый']


def test_pick_facts_takes_the_topic_the_freshest_and_some_random():
    facts = [storage.Fact(i, 'who', f'факт {i}', f'k{i:02d}', 1) for i in range(10)]
    facts.append(storage.Fact(10, 'interest', 'фанат варика', 'k00', 1))
    picked = storage.pick_facts(facts, 5, 'го в варик')
    assert len(picked) == 5 and len(set(picked)) == 5
    assert facts[10] in picked                    # on the topic of the question
    assert facts[9] in picked and facts[8] in picked     # the freshest
    assert storage.pick_facts(facts[:3], 5) and len(storage.pick_facts(facts[:3], 5)) == 3


async def test_dossier_answer_is_checked(db, monkeypatch):
    """No quotes in facts, own ids only, a known kind, real nicks in relations."""
    await _say('kent', 'привет', 1)
    known = [storage.Fact(7, 'who', 'старый', 'k', 1)]
    result = build._Dossier.model_validate_json(_dossier_answer(
        headline='«сын фермера» из Краснодара',
        new_facts=[{'kind': 'who', 'text': 'пишет «билет 30к» каждый стрим'},
                   {'kind': 'whatever', 'text': 'фанат варика'}, {'kind': 'who', 'text': '«»'}],
        confirmed=[7, 99], outdated=[99],
        relations=[{'nick': '@kent', 'note': 'кореш'}, {'nick': 'putin', 'note': 'политик'}],
        catchphrases=['«ну че»', 'ну че', 'x' * 200, 'попался джсер', 'третья'],
    ))
    dossier = await build._dossier('gop', 'k2', 3, result, known)
    assert dossier.new == [('who', 'пишет билет 30к каждый стрим'), ('who', 'фанат варика')]
    assert dossier.confirmed == [7] and dossier.outdated == []
    assert dossier.profile.portrait == 'сын фермера из Краснодара'
    assert dossier.profile.relations == [{'nick': 'kent', 'note': 'кореш'}]
    assert dossier.profile.catchphrases == ('ну че', 'попался джсер')


async def test_conversation_updates_an_existing_dossier(db, monkeypatch):
    await _say('gop', 'опять варик', 5)
    profile = storage.Profile('gop', 'шапка', [], 'k1', 1)
    await storage.apply_dossier(profile, [('interest', 'варик')], [], [], 40)
    (wc3,) = await storage.get_facts('gop')
    _gemini(monkeypatch, _dossier_answer(confirmed=[wc3.id], new_facts=[{'kind': 'who', 'text': 'новое'}]))
    block = (await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False))[-1]
    assert await build.update_profile('gop', block, None) is not None
    assert {f.text: f.hits for f in await storage.get_facts('gop')} == {'варик': 2, 'новое': 1}
    # The model is shown the ids, so it can confirm a fact instead of writing it again
    assert f'[{wc3.id}] (interest) варик' in build._dossier_text(profile, [wc3])


@pytest.mark.parametrize('headline, whole_history', [
    (None, True), ('тихий, пишет редко', False), ('старый абзац. ' * 70, True),
])
async def test_only_a_chatter_without_a_dossier_gets_the_whole_history(db, monkeypatch, headline, whole_history):
    """No dossier yet – the first update reads all their history, as the backfill does. A quiet
    chatter's dossier may hold a headline and no fact: it is done, and only grows by the conversation.
    An old paragraph profile is no headline: it is rebuilt."""
    await _say('gop', 'давнее сообщение', 30)
    await _say('gop', 'свежее сообщение', 5)
    old = storage.Profile('gop', headline, [], 'old', 5) if headline else None
    if old:
        await storage.save_profile(old)
    _gemini(monkeypatch, _dossier_answer(new_facts=[{'kind': 'who', 'text': 'фанат варика'}]))
    written = []

    async def spy(username, *, profile, facts, messages, **kwargs):
        written.append((profile, messages))
        return await write(username, profile=profile, facts=facts, messages=messages, **kwargs)
    write = build._write_dossier
    monkeypatch.setattr(build, '_write_dossier', spy)
    block = (await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False))[-1]
    profile = await build.update_profile('gop', block, None)
    expected = ['давнее сообщение', 'свежее сообщение'] if whole_history else ['свежее сообщение']
    assert written == [(None if whole_history else old, expected)]
    assert profile.portrait == 'шапка'
    assert [f.text for f in await storage.get_facts('gop')] == ['фанат варика']
    assert await storage.users_with_dossier() == {'gop'}


async def test_an_old_paragraph_profile_is_not_a_dossier(db):
    """--build-memory rebuilds it: shown whole, it is the quotes the bot pastes back."""
    await storage.save_profile(storage.Profile('old', 'абзац. ' * 150, [], 'k', 3))
    await storage.save_profile(storage.Profile('new', 'шапка', [], 'k', 3))
    assert await storage.users_with_dossier() == {'new'}


async def test_a_first_dossier_reads_nothing_after_its_conversation(db, monkeypatch):
    """The later conversations feed their own messages in when the memory reaches them."""
    await _say('gop', 'давнее сообщение', 30)
    await _say('gop', 'позднее сообщение', 5)
    _gemini(monkeypatch, _dossier_answer())
    written = []

    async def spy(username, *, messages, **kwargs):
        written.append(messages)
        return await write(username, messages=messages, **kwargs)
    write = build._write_dossier
    monkeypatch.setattr(build, '_write_dossier', spy)
    first = (await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False))[0]
    assert await build.first_profile('gop', first) is not None
    assert written == [['давнее сообщение']]


async def test_a_repeated_fact_is_a_confirmation(db):
    """The conversation the dossier was built from is read again when it ends: a fact
    worded the same is confirmed, not added twice."""
    profile = storage.Profile('gop', '', [], 'k1', 1)
    await storage.apply_dossier(profile, [('interest', 'Фанат варика.')], [], [], 40)
    await storage.apply_dossier(profile._replace(last_conversation='k2'),
                                [('interest', 'фанат варика'), ('who', 'новое'), ('who', 'Новое!')], [], [], 40)
    assert {f.text: f.hits for f in await storage.get_facts('gop')} == {'Фанат варика.': 2, 'новое': 1}


async def test_a_rebuilt_dossier_keeps_a_fact_it_repeats(db):
    """A full rebuild retires the old facts: a new one worded like a retired one is kept."""
    profile = storage.Profile('gop', '', [], 'k1', 1)
    await storage.apply_dossier(profile, [('interest', 'фанат варика')], [], [], 40)
    (old,) = await storage.get_facts('gop')
    await storage.apply_dossier(profile, [('interest', 'фанат варика')], [], [old.id], 40)
    (kept,) = await storage.get_facts('gop')
    assert kept.text == 'фанат варика' and kept.id != old.id


def test_memory_config_takes_the_thinking_budget(monkeypatch):
    """A hand-built config pinned thinking to 0, which a thinking-only model rejects."""
    monkeypatch.setattr(build.Gemini, 'THINKING_BUDGET', 512)
    config = build._config(build._Dossier, 100)
    assert config.thinking_config.thinking_budget == 512
    assert config.response_schema is build._Dossier and config.max_output_tokens == 100 + 512
    assert config.system_instruction is None


@pytest.mark.parametrize('budget, extra', [(0, 0), (512, 512), (-1, build.DYNAMIC_THINKING_TOKENS)])
def test_thinking_does_not_eat_the_answer_cap(monkeypatch, budget, extra):
    """Thinking tokens count against max_output_tokens: a raised budget must not cut the JSON."""
    monkeypatch.setattr(Gemini, 'THINKING_BUDGET', budget)
    assert build._config(build._Dossier, build.PROFILE_MAX_TOKENS).max_output_tokens == build.PROFILE_MAX_TOKENS + extra
