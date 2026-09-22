"""!who / !versus material: what the model is told about the target."""
from src.core.database import get_db
from src.gemini import who


async def _fact(author: str, fact: str) -> None:
    db = await get_db()
    await db.execute('INSERT INTO facts (username, fact) VALUES (?, ?)', (author, fact))
    await db.commit()


async def test_facts_are_the_ones_about_the_target(db):
    """facts.username is the author: the label «facts about {target}» must get facts that
    name the target, not whatever the target wrote about other people."""
    await _fact('exitfound', '@nosok222 опять проспал стрим')
    await _fact('nosok222', '@ne2oi жрёт медопепе')
    about = await who.material('nosok222', 1, 10)
    assert about.facts == [('exitfound', '@nosok222 опять проспал стрим')]
    assert (await who.material('exitfound', 1, 10)).facts == []


async def test_a_nick_prefix_is_not_the_nick(db):
    await _fact('a', '@nosok2222 другой человек')
    assert (await who.material('nosok222', 1, 10)).facts == []


def _material(n: int) -> who.Material:
    lines = [f'строка{i:02d}' for i in range(n)]
    return who.Material('gop', [('a', f'факт{i:02d}') for i in range(n)], lines, lines, lines,
                        lines, lines, [f'диалог{i:02d}' for i in range(n)])


def test_a_smaller_rung_cuts_every_block():
    """The input filter may object to any block: the dialogues, facts and recent
    messages shrink along with the sample."""
    text = _material(10).add_sample(who.ContextBuilder(), 0.4).build()
    assert text.count('факт') == 4
    assert text.count('диалог') == 4 and 'диалог09' in text and 'диалог05' not in text


def test_a_non_empty_block_keeps_one_line():
    assert who.cut(['один'], 0.4) == ['один']
    assert who.cut([], 0.4) == []


def test_the_last_rung_is_the_last_messages_alone():
    text = _material(3).add_last(who.ContextBuilder()).build()
    assert 'строка02' in text and 'факт' not in text and 'диалог' not in text


async def test_versus_takes_half_the_dialogues(db):
    db_ = await get_db()
    for i in range(10):
        await db_.execute('INSERT INTO bot_interactions (session_id, username, user_message, bot_response)'
                          ' VALUES (?, ?, ?, ?)', ('s', 'gop', f'вопрос {i}', f'ответ {i}'))
    await db_.commit()
    assert len((await who.material('gop', 1, 10)).dialogue) == 10
    assert len((await who.material('gop', 0.5, 10)).dialogue) == 5
