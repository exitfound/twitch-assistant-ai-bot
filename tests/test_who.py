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
