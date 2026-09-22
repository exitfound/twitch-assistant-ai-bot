"""The «залупа стрима» game on a temporary database: throws, rewards, curses, perks.

Channel points depend on this module, so every documented rule has a test here.
"""
import asyncio
import time

import pytest

from src.core.config import Rewards, Roll
from src.core.database import get_db, save_chat_message, save_stream
from src.local.roll import game
from src.local.roll.storage import (
    RollRow, add_perk, get_pending_perk_users, get_perk, get_roll, get_session_champion,
    get_session_loser, save_roll, set_curse,
)

S = '2026-09-22 20:00'
_ids = iter(range(1, 10_000))


def redeem(action: str, actor: str, user_input: str = '', session: str = S):
    return game.redeem(action, session, actor, user_input, f'redemption-{next(_ids)}')


@pytest.fixture
def top(monkeypatch):
    """Every throw lands on its ceiling: randint(a, b) == b."""
    monkeypatch.setattr(game.random, 'randint', lambda a, b: b)


@pytest.fixture
def fixed(monkeypatch):
    """Throws return the values put into the returned list, in order."""
    values: list[int] = []
    monkeypatch.setattr(game.random, 'randint', lambda a, b: values.pop(0))
    return values


# --- free throws ---------------------------------------------------------------

async def test_free_throws_run_out(db):
    left = [(await game.free_throw(S, 'gop', limit=3)).free_left for _ in range(3)]
    assert left == [2, 1, 0]
    refused = await game.free_throw(S, 'gop', limit=3)
    assert refused.status == game.Status.NO_FREE_LEFT
    assert (await get_roll(S, 'gop')).free_throws == 3


async def test_broadcaster_is_unlimited_but_counted(db):
    for _ in range(5):
        outcome = await game.free_throw(S, 'streamer', limit=3, unlimited=True)
        assert outcome.ok and outcome.free_left is None
    assert (await get_roll(S, 'streamer')).free_throws == 5


async def test_extra_is_refunded_while_free_throws_remain(db):
    await game.free_throw(S, 'gop', limit=3)
    outcome = await redeem(game.Action.EXTRA, 'gop')
    assert outcome.status == game.Status.FREE_LEFT
    assert outcome.free_left == 2


async def test_extra_uses_the_limit_stored_by_the_chat_throw(db):
    """A redemption carries no badges: a subscriber's limit comes from their row."""
    await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)
    outcome = await redeem(game.Action.EXTRA, 'sub')
    assert outcome.status == game.Status.FREE_LEFT
    assert outcome.free_left == Roll.FREE_SUB - 1


async def test_extra_after_free_throws_does_not_spend_them(db):
    for _ in range(3):
        await game.free_throw(S, 'gop', limit=3)
    outcome = await redeem(game.Action.EXTRA, 'gop')
    assert outcome.ok
    assert (await get_roll(S, 'gop')).free_throws == 3


# --- loser and champion --------------------------------------------------------

async def test_loser_and_champion(db, fixed):
    fixed += [40, 90, 10]
    for user in ('a', 'b', 'c'):
        outcome = await game.free_throw(S, user, limit=3)
    assert outcome.loser == ('c', 10)
    assert outcome.champion == ('b', 90)


async def test_single_player_is_not_their_own_champion(db):
    outcome = await game.free_throw(S, 'alone', limit=3)
    assert outcome.loser[0] == 'alone'
    assert outcome.champion is None


async def test_equal_rolls_go_to_whoever_threw_first(db):
    await save_roll(S, 'first', 50, free_throw=True)
    await save_roll(S, 'second', 50, free_throw=True)
    assert await get_session_loser(S) == ('first', 50)
    assert await get_session_champion(S) == ('first', 50)


# --- curse -------------------------------------------------------------------

async def test_curse_ladder_reaches_the_floor_and_holds(db, top):
    await save_roll(S, 'victim', 90, free_throw=True)
    cursed = await redeem(game.Action.CURSE, 'actor', '@Victim')
    assert cursed.ok
    assert (cursed.ceiling, cursed.value, cursed.next_ceiling) == (
        Rewards.CURSE_CEILING, Rewards.CURSE_CEILING, Rewards.CURSE_CEILING - Rewards.CURSE_STEP,
    )
    ceiling = cursed.next_ceiling
    while ceiling > Rewards.CURSE_FLOOR:
        outcome = await game.free_throw(S, 'victim', limit=3, unlimited=True)
        assert outcome.value == outcome.ceiling == ceiling
        ceiling = outcome.next_ceiling
    floor_at = (await get_roll(S, 'victim')).curse_floor_at
    assert floor_at is not None
    assert outcome.curse_minutes_left == Rewards.CURSE_HOLD_MINUTES

    held = await game.free_throw(S, 'victim', limit=3, unlimited=True)
    assert held.ceiling == held.next_ceiling == Rewards.CURSE_FLOOR
    assert (await get_roll(S, 'victim')).curse_floor_at == floor_at


def test_curse_expires_on_the_hold_and_on_the_deadline(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(game.time, 'time', lambda: now)
    hold = Rewards.CURSE_HOLD_MINUTES * 60
    row = RollRow(value=30, free_throws=1, curse_ceiling=25, curse_floor_at=None, curse_until=None, free_limit=3)
    assert game._curse_of(row) == (25, None)
    assert game._curse_of(row._replace(curse_floor_at=now - hold + 1)) is not None
    assert game._curse_of(row._replace(curse_floor_at=now - hold)) is None
    assert game._curse_of(row._replace(curse_until=now)) is None
    assert game._curse_of(row._replace(curse_ceiling=None)) is None
    assert game._curse_of(None) is None


async def test_lifted_curse_is_reported_once(db):
    await save_roll(S, 'victim', 20, free_throw=True)
    await set_curse(S, 'victim', Rewards.CURSE_FLOOR, time.time() - Rewards.CURSE_HOLD_MINUTES * 60 - 1)
    await save_roll(S, 'fresh', 20, free_throw=True)
    await set_curse(S, 'fresh', Rewards.CURSE_FLOOR, time.time())
    assert await game.lift_expired_curses(S) == ['victim']
    assert await game.lift_expired_curses(S) == []
    assert (await get_roll(S, 'fresh')).curse_ceiling == Rewards.CURSE_FLOOR


@pytest.mark.parametrize(('user_input', 'status'), [
    ('!!!', game.Status.BAD_TARGET),
    ('', game.Status.BAD_TARGET),
    ('@Actor,', game.Status.SELF_TARGET),
    ('nobody', game.Status.NOT_ROLLED),
])
async def test_curse_refusals(db, user_input, status):
    assert (await redeem(game.Action.CURSE, 'actor', user_input)).status == status


async def test_curse_is_not_laid_twice(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'a', 'victim')).ok
    assert (await redeem(game.Action.CURSE, 'b', 'victim')).status == game.Status.ALREADY_CURSED


async def test_curse_pierces_every_protection(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.SHIELD, 'victim')).ok
    await add_perk(S, 'victim', game.Perk.SHIELD, 'previous')
    await game.appear(S, 'victim')
    assert (await redeem(game.Action.CURSE, 'actor', 'victim')).ok


# --- reroll ------------------------------------------------------------------

async def test_reroll_replaces_the_target_roll_without_spending_free_throws(db, fixed):
    await save_roll(S, 'victim', 90, free_throw=True)
    fixed.append(5)
    outcome = await redeem(game.Action.REROLL, 'actor', '@victim')
    assert (outcome.status, outcome.old_value, outcome.value) == (game.Status.OK, 90, 5)
    assert (await get_roll(S, 'victim')).free_throws == 1


async def test_reroll_on_a_cursed_target_lowers_the_ceiling(db, top):
    await save_roll(S, 'victim', 90, free_throw=True)
    await redeem(game.Action.CURSE, 'a', 'victim')
    outcome = await redeem(game.Action.REROLL, 'b', 'victim')
    assert outcome.value == outcome.ceiling == Rewards.CURSE_CEILING - Rewards.CURSE_STEP


async def test_reroll_of_someone_who_never_played(db):
    assert (await redeem(game.Action.REROLL, 'actor', 'ghost')).status == game.Status.UNKNOWN_TARGET
    await save_chat_message(S, 'lurker', 'привет')
    outcome = await redeem(game.Action.REROLL, 'actor', 'lurker')
    assert outcome.ok and outcome.old_value is None


async def test_bought_shield_blocks_rerolls(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.SHIELD, 'victim')).ok
    assert (await redeem(game.Action.SHIELD, 'victim')).status == game.Status.ALREADY_SHIELDED
    assert (await redeem(game.Action.REROLL, 'actor', 'victim')).status == game.Status.SHIELDED


async def test_perk_shield_blocks_rerolls_with_minutes(db):
    await save_roll(S, 'champ', 99, free_throw=True)
    await add_perk(S, 'champ', game.Perk.SHIELD, 'previous')
    outcome = await redeem(game.Action.REROLL, 'actor', 'champ')
    assert outcome.status == game.Status.PERK_SHIELDED
    assert outcome.protect_minutes_left == Roll.PERK_MINUTES


async def test_protection_after_a_reroll(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.REROLL, 'a', 'victim')).ok
    second = await redeem(game.Action.REROLL, 'b', 'victim')
    assert second.status == game.Status.PROTECTED
    assert second.protect_minutes_left == Rewards.REROLL_PROTECT_MINUTES


async def test_refused_rerolls_do_not_extend_the_protection(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.REROLL, 'a', 'victim')).ok
    assert (await redeem(game.Action.REROLL, 'b', 'victim')).status == game.Status.PROTECTED
    db_ = await get_db()
    # The successful reroll moves out of the window; the refused one stays fresh
    await db_.execute(
        "UPDATE roll_actions SET created_at = datetime('now', ?) WHERE status = 'ok'",
        (f'-{Rewards.REROLL_PROTECT_MINUTES + 1} minutes',),
    )
    await db_.commit()
    assert (await redeem(game.Action.REROLL, 'c', 'victim')).ok


# --- journal -----------------------------------------------------------------

async def test_duplicate_redemption_changes_nothing(db, fixed, monkeypatch):
    await save_roll(S, 'victim', 90, free_throw=True)
    fixed.append(40)
    first = await game.redeem(game.Action.REROLL, S, 'actor', 'victim', 'same-id')
    assert first.ok
    monkeypatch.setattr(game.random, 'randint', lambda a, b: pytest.fail('a duplicate must not throw'))
    again = await game.redeem(game.Action.REROLL, S, 'actor', 'victim', 'same-id')
    assert again.status == game.Status.DUPLICATE
    assert (await get_roll(S, 'victim')).value == 40
    async with (await get_db()).execute('SELECT COUNT(*) FROM roll_actions') as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_concurrent_operations_lose_no_update(db, top):
    """The reason game._lock exists: a reroll between the read and the write of the
    victim's own throw would otherwise overwrite one of them."""
    await save_roll(S, 'victim', 90, free_throw=True)
    await asyncio.gather(
        game.free_throw(S, 'victim', limit=10),
        redeem(game.Action.REROLL, 'a', 'victim'),
        redeem(game.Action.CURSE, 'b', 'victim'),
    )
    row = await get_roll(S, 'victim')
    assert row.free_throws == 2
    # The curse came first, second or last: each throw after it lowered the ceiling once
    assert row.curse_ceiling in {Rewards.CURSE_CEILING - n * Rewards.CURSE_STEP for n in (1, 2, 3)}


# --- status and perks --------------------------------------------------------

async def test_status_reads_without_starting_a_perk(db):
    await save_roll(S, 'champ', 99, free_throw=True)
    await add_perk(S, 'champ', game.Perk.SHIELD, 'previous')
    standing = await game.status(S, 'champ', limit=3)
    assert standing.value == 99
    assert standing.free_left == 2
    assert standing.shield_minutes_left is None
    assert 'champ' in await get_pending_perk_users(S)


async def _previous_stream(rolls: dict[str, int]) -> None:
    now = time.time()
    await save_stream('stream-1', 'P', now - 86_400)
    await save_stream('stream-2', S, now - 60)
    for user, value in rolls.items():
        await save_roll('P', user, value, free_throw=True)


async def test_perks_are_granted_once(db):
    await _previous_stream({'champ': 95, 'loser': 3, 'middle': 50})
    assert await game.grant_perks(S) == ('champ', 'loser')
    assert await game.grant_perks(S) is None


async def test_single_player_gets_only_the_curse(db):
    await _previous_stream({'alone': 50})
    assert await game.grant_perks(S) == (None, 'alone')


async def test_no_previous_rolls_no_perks(db):
    await _previous_stream({})
    assert await game.grant_perks(S) is None


async def test_perk_countdown_starts_once(db):
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    assert await game.appear(S, 'loser') == [game.Perk.CURSE]
    assert await game.appear(S, 'loser') == []


async def test_perk_curse_is_laid_on_the_first_throw_once(db, top):
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    first = await game.free_throw(S, 'loser', limit=3)
    assert first.ceiling == Rewards.CURSE_CEILING
    row = await get_roll(S, 'loser')
    assert row.curse_until is not None
    assert (await get_perk(S, 'loser', game.Perk.CURSE)).consumed

    await set_curse(S, 'loser', None, None)
    second = await game.free_throw(S, 'loser', limit=3)
    assert second.ceiling is None


@pytest.mark.parametrize(('raw', 'nick'), [
    ('@Nick', 'nick'), ('nick, и ещё', 'nick'), ('Nick!', 'nick'), ('ник', None), ('', None), ('x' * 26, None),
])
def test_parse_nick(raw, nick):
    assert game.parse_nick(raw) == nick


def test_minutes_left_take_the_earlier_deadline(monkeypatch):
    """The previous stream's loser curse also ends at curse_until: announcing the floor's
    full hold would promise a lift time the curse never reaches."""
    now = 1_000_000.0
    monkeypatch.setattr(game.time, 'time', lambda: now)
    hold = Rewards.CURSE_HOLD_MINUTES
    assert game._minutes_left(now - 60) == hold - 1
    assert game._minutes_left(now - 60, now + 5 * 60) == 5
    assert game._minutes_left(now - 60, now + 3600) == hold - 1
    assert game._minutes_left(None, now + 5 * 60) is None


async def test_perk_curse_on_the_floor_reports_its_deadline(db, top, monkeypatch):
    monkeypatch.setattr(Roll, 'PERK_MINUTES', 5)
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    outcome = await game.free_throw(S, 'loser', limit=3, unlimited=True)
    while outcome.next_ceiling > Rewards.CURSE_FLOOR:
        outcome = await game.free_throw(S, 'loser', limit=3, unlimited=True)
    assert outcome.curse_minutes_left == 5
    standing = await game.status(S, 'loser', limit=3)
    assert standing.curse_minutes_left == 5
