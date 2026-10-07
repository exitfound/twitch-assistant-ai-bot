"""The «залупа стрима» game on a temporary database: throws, rewards, curses, perks.

Channel points depend on this module, so every documented rule has a test here.
"""
import asyncio
import time

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.twitch.core.commands import CommandContext
from src.core.config import Rewards, Roll, Twitch
from src.twitch.core.viewer import Tier
from src.core.database import get_db, save_chat_message, save_stream
from src.twitch.local.roll import game, rules
from src.twitch.local.roll.command import handle_roll, handle_rollstat
from src.twitch.local.roll.storage import (
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
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: b)


@pytest.fixture
def fixed(monkeypatch):
    """Throws return the values put into the returned list, in order."""
    values: list[int] = []
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: values.pop(0))
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


async def _age_throws(seconds: float) -> None:
    """Move every recorded throw `seconds` into the past."""
    db_ = await get_db()
    await db_.execute('UPDATE roll_throws SET thrown_at = thrown_at - ?', (seconds,))
    await db_.commit()


async def test_free_and_extra_throws_count_together_in_a_burst(db):
    """Three free !roll and two extra rolls within a minute: the next one waits."""
    for _ in range(3):
        assert (await game.free_throw(S, 'gop', limit=3)).ok
    for _ in range(Roll.BURST_THROWS - 3):
        assert (await redeem(game.Action.EXTRA, 'gop')).ok
    refused = await redeem(game.Action.EXTRA, 'gop')
    assert refused.status == game.Status.TOO_FAST
    assert refused.protect_minutes_left == Roll.BURST_PAUSE_MINUTES


async def test_a_burst_of_free_throws_waits_and_keeps_the_throw(db):
    """A subscriber has no cooldown and ten free throws: five in a row, then a pause."""
    for _ in range(Roll.BURST_THROWS):
        assert (await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)).ok
    refused = await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)
    assert refused.status == game.Status.TOO_FAST
    assert (await get_roll(S, 'sub')).free_throws == Roll.BURST_THROWS
    # The pause runs from the last throw: once it has passed, the game is open again
    await _age_throws(Roll.BURST_PAUSE_MINUTES * 60)
    assert (await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)).ok


async def test_throws_spread_over_more_than_the_window_are_not_a_burst(db):
    for _ in range(Roll.BURST_THROWS):
        assert (await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)).ok
        await _age_throws(Roll.BURST_SECONDS / (Roll.BURST_THROWS - 1) + 1)
    assert (await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)).ok


async def test_the_broadcaster_is_not_held(db):
    """!roll knows the broadcaster by the badge, a redemption by the channel's login."""
    streamer = Twitch.CHANNEL.lower()
    for _ in range(Roll.BURST_THROWS + 1):
        assert (await game.free_throw(S, streamer, limit=3, unlimited=True)).ok
    for _ in range(Roll.BURST_THROWS):
        assert (await redeem(game.Action.EXTRA, streamer)).ok


@pytest.mark.parametrize(('times', 'now', 'left'), [
    ([40, 30, 20, 10], 45, None),               # four throws are not a burst yet
    ([40, 30, 20, 10, 0], 50, 170),             # five within the window: the pause runs from the newest
    ([60, 45, 30, 15, 0], 70, 170),             # exactly the window still counts
    ([61, 45, 30, 15, 0], 70, None),            # spread wider than the window
    ([40, 30, 20, 10, 0], 220, None),           # the pause has passed
])
def test_burst_pause(times, now, left):
    assert rules.burst_pause_left(times, now, 5, 60, 180) == left


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


async def test_equal_rolls_go_to_whoever_threw_last(db, fixed):
    """Matching the loser makes you the loser, matching the best makes you the best."""
    fixed += [9, 9, 90, 90]
    await game.free_throw(S, 'a', limit=3)
    outcome = await game.free_throw(S, 'b', limit=3)
    assert outcome.loser == ('b', 9)
    await game.free_throw(S, 'c', limit=3)
    outcome = await game.free_throw(S, 'd', limit=3)
    assert outcome.champion == ('d', 90)


async def test_the_last_throw_wins_a_tie_within_one_second(db):
    """rolled_at keeps milliseconds: to the second, «last» would fall back to the row id,
    which is whoever joined the game later, not whoever threw later."""
    await save_roll(S, 'early', 50, free_throw=True)
    await save_roll(S, 'late', 50, free_throw=True)
    await asyncio.sleep(0.01)
    await save_roll(S, 'early', 50, free_throw=True)
    assert await get_session_loser(S) == ('early', 50)
    assert await get_session_champion(S) == ('early', 50)


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


def test_curse_expires_on_the_hold_and_on_the_deadline():
    now = 1_000_000.0
    hold = Rewards.CURSE_HOLD_MINUTES * 60
    row = RollRow(value=30, free_throws=1, curse_ceiling=25, curse_floor_at=None, curse_until=None, free_limit=3)
    assert rules.curse_of(row, now) == (25, None)
    assert rules.curse_of(row._replace(curse_floor_at=now - hold + 1), now) is not None
    assert rules.curse_of(row._replace(curse_floor_at=now - hold), now) is None
    assert rules.curse_of(row._replace(curse_until=now), now) is None
    assert rules.curse_of(row._replace(curse_ceiling=None), now) is None
    assert rules.curse_of(None, now) is None


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
    ('victim other', game.Status.EXTRA_WORDS),
    ('@victim спасибо', game.Status.EXTRA_WORDS),
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

async def _targets(count: int) -> list[str]:
    """Players to reroll: a different one each time, past the target's own protection."""
    names = [f'target{i}' for i in range(count)]
    for name in names:
        await save_roll(S, name, 50, free_throw=True)
    return names


async def _rerolls(actor: str, count: int) -> list[game.Outcome]:
    return [await redeem(game.Action.REROLL, actor, target) for target in await _targets(count)]


@pytest.mark.parametrize(('tier', 'limit'), [
    (None, Rewards.LIMIT_FOLLOWER),            # never rolled this session: a follower
    (Tier.REGULAR, Rewards.LIMIT_FOLLOWER),
    (Tier.VIP, Rewards.LIMIT_VIP),
    (Tier.SUB, Rewards.LIMIT_SUB),
    (Tier.MODERATOR, Rewards.LIMIT_SUB),
])
async def test_rerolls_per_window_follow_the_status_of_the_last_roll(db, tier, limit):
    if tier is not None:
        await game.free_throw(S, 'buyer', limit=3, tier=tier)
    *done, refused = await _rerolls('buyer', limit + 1)
    assert all(outcome.ok for outcome in done)
    assert refused.status == game.Status.REROLL_LIMIT
    assert refused.limit == limit
    assert refused.protect_minutes_left == Rewards.REROLL_WINDOW_MINUTES


async def test_a_new_window_opens_when_the_first_one_ends(db):
    await _rerolls('buyer', Rewards.LIMIT_FOLLOWER)
    db_ = await get_db()
    await db_.execute("UPDATE roll_actions SET created_at = datetime('now', ?)",
                      (f'-{Rewards.REROLL_WINDOW_MINUTES} minutes',))
    await db_.commit()
    assert all(outcome.ok for outcome in await _rerolls('buyer', Rewards.LIMIT_FOLLOWER))


async def test_a_refused_reroll_does_not_use_up_the_window(db):
    """A shield or a typo gives the points back: no reroll happened, none is counted."""
    await save_roll(S, 'guarded', 50, free_throw=True)
    await redeem(game.Action.SHIELD, 'guarded')
    for _ in range(Rewards.LIMIT_FOLLOWER):
        assert (await redeem(game.Action.REROLL, 'buyer', 'guarded')).status == game.Status.SHIELDED
    assert all(outcome.ok for outcome in await _rerolls('buyer', Rewards.LIMIT_FOLLOWER))


async def test_a_status_gained_mid_stream_counts_from_the_next_roll(db):
    """Even a refused !roll refreshes the status: the badges came with it."""
    for _ in range(3):
        await game.free_throw(S, 'buyer', limit=3, tier=Tier.REGULAR)
    refused = await game.free_throw(S, 'buyer', limit=3, tier=Tier.SUB)
    assert refused.status == game.Status.NO_FREE_LEFT
    *done, last = await _rerolls('buyer', Rewards.LIMIT_SUB + 1)
    assert all(outcome.ok for outcome in done)
    assert last.status == game.Status.REROLL_LIMIT


async def test_the_broadcaster_rerolls_without_a_window(db):
    outcomes = await _rerolls(Twitch.CHANNEL.lower(), Rewards.LIMIT_SUB + 2)
    assert all(outcome.ok for outcome in outcomes)


@pytest.mark.parametrize(('times', 'now', 'left'), [
    ([], 0, None),
    ([0, 100], 200, None),                      # two of three
    ([0, 100, 200], 300, 1500),                 # the window opened at 0 is full
    ([0, 100, 200], 1800, None),                # it has ended
    ([0, 100, 200, 1800, 1900], 2000, None),    # a new window, two of three
    ([0, 100, 200, 1800, 1900, 2000], 2100, 1500),
])
def test_reroll_window(times, now, left):
    assert rules.window_left(times, now, 3, 1800) == left


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
    again = await redeem(game.Action.SHIELD, 'victim')
    assert again.status == game.Status.ALREADY_SHIELDED
    assert again.protect_minutes_left == Rewards.SHIELD_MINUTES
    blocked = await redeem(game.Action.REROLL, 'actor', 'victim')
    assert blocked.status == game.Status.SHIELDED
    assert blocked.protect_minutes_left == Rewards.SHIELD_MINUTES


async def test_bought_shield_runs_out_and_can_be_bought_again(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.SHIELD, 'victim')).ok
    db_ = await get_db()
    await db_.execute(
        "UPDATE roll_actions SET created_at = datetime('now', ?) WHERE action = 'shield'",
        (f'-{Rewards.SHIELD_MINUTES} minutes',),
    )
    await db_.commit()
    assert (await game.status(S, 'victim', limit=3)).shield_left is None
    assert (await redeem(game.Action.REROLL, 'actor', 'victim')).ok
    assert (await redeem(game.Action.SHIELD, 'victim')).ok
    assert (await game.status(S, 'victim', limit=3)).shield_left == Rewards.SHIELD_MINUTES


async def _expire_shields() -> None:
    db_ = await get_db()
    await db_.execute("UPDATE roll_actions SET created_at = datetime('now', ?) WHERE action = 'shield'",
                      (f'-{Rewards.SHIELD_MINUTES} minutes',))
    await db_.commit()


@pytest.mark.parametrize(('tier', 'limit'), [
    (None, Rewards.LIMIT_FOLLOWER),
    (Tier.VIP, Rewards.LIMIT_VIP),
    (Tier.SUB, Rewards.LIMIT_SUB),
])
async def test_shields_per_stream_follow_the_status_of_the_last_roll(db, tier, limit):
    """The limit is per stream: once used up, an expired shield cannot be bought again."""
    if tier is not None:
        await game.free_throw(S, 'buyer', limit=3, tier=tier)
    for _ in range(limit):
        assert (await redeem(game.Action.SHIELD, 'buyer')).ok
        await _expire_shields()
    refused = await redeem(game.Action.SHIELD, 'buyer')
    assert refused.status == game.Status.SHIELD_LIMIT
    assert refused.limit == limit


async def test_a_refused_shield_does_not_use_up_the_limit(db):
    assert (await redeem(game.Action.SHIELD, 'buyer')).ok
    for _ in range(Rewards.LIMIT_FOLLOWER):
        assert (await redeem(game.Action.SHIELD, 'buyer')).status == game.Status.ALREADY_SHIELDED
    for _ in range(Rewards.LIMIT_FOLLOWER - 1):
        await _expire_shields()
        assert (await redeem(game.Action.SHIELD, 'buyer')).ok


async def test_the_broadcaster_buys_shields_without_a_limit(db):
    for _ in range(Rewards.LIMIT_SUB + 1):
        assert (await redeem(game.Action.SHIELD, Twitch.CHANNEL.lower())).ok
        await _expire_shields()


async def test_the_champions_shield_counts_as_a_shield(db):
    """A bought shield on top of the champion's adds nothing: the points come back."""
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    refused = await redeem(game.Action.SHIELD, 'champ')
    assert refused.status == game.Status.ALREADY_SHIELDED
    assert refused.protect_minutes_left == Roll.PERK_MINUTES


async def _curses(actor: str, count: int) -> list[game.Outcome]:
    return [await redeem(game.Action.CURSE, actor, target) for target in await _targets(count)]


@pytest.mark.parametrize(('tier', 'limit'), [
    (None, Rewards.LIMIT_FOLLOWER),
    (Tier.VIP, Rewards.LIMIT_VIP),
    (Tier.SUB, Rewards.LIMIT_SUB),
])
async def test_curses_per_stream_follow_the_status_of_the_last_roll(db, tier, limit):
    if tier is not None:
        await game.free_throw(S, 'buyer', limit=3, tier=tier)
    *done, refused = await _curses('buyer', limit + 1)
    assert all(outcome.ok for outcome in done)
    assert refused.status == game.Status.CURSE_LIMIT
    assert refused.limit == limit


async def test_a_refused_curse_does_not_use_up_the_limit(db):
    """Cursing someone already cursed gives the points back: no curse, none counted."""
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'other', 'victim')).ok
    for _ in range(Rewards.LIMIT_FOLLOWER):
        assert (await redeem(game.Action.CURSE, 'buyer', 'victim')).status == game.Status.ALREADY_CURSED
    assert all(outcome.ok for outcome in await _curses('buyer', Rewards.LIMIT_FOLLOWER))


async def test_the_broadcaster_curses_without_a_limit(db):
    assert all(outcome.ok for outcome in await _curses(Twitch.CHANNEL.lower(), Rewards.LIMIT_SUB + 1))


async def _cleanses(actor: str, count: int) -> list[game.Outcome]:
    """Cleanse `count` players, each cursed first by the broadcaster, who has no limit."""
    outcomes = []
    for target in await _targets(count):
        assert (await redeem(game.Action.CURSE, Twitch.CHANNEL.lower(), target)).ok
        outcomes.append(await redeem(game.Action.CLEANSE, actor, target))
    return outcomes


@pytest.mark.parametrize(('tier', 'limit'), [
    (None, Rewards.LIMIT_FOLLOWER),
    (Tier.VIP, Rewards.LIMIT_VIP),
    (Tier.SUB, Rewards.LIMIT_SUB),
])
async def test_cleanses_per_stream_follow_the_status_of_the_last_roll(db, tier, limit):
    if tier is not None:
        await game.free_throw(S, 'buyer', limit=3, tier=tier)
    *done, refused = await _cleanses('buyer', limit + 1)
    assert all(outcome.ok for outcome in done)
    assert refused.status == game.Status.CLEANSE_LIMIT
    assert refused.limit == limit


async def test_a_refused_cleanse_does_not_use_up_the_limit(db):
    """Cleansing someone with no curse gives the points back: nothing lifted, none counted."""
    await save_roll(S, 'clean', 50, free_throw=True)
    for _ in range(Rewards.LIMIT_FOLLOWER):
        assert (await redeem(game.Action.CLEANSE, 'buyer', 'clean')).status == game.Status.NOT_CURSED
    assert all(outcome.ok for outcome in await _cleanses('buyer', Rewards.LIMIT_FOLLOWER))


async def test_the_broadcaster_cleanses_without_a_limit(db):
    assert all(outcome.ok for outcome in await _cleanses(Twitch.CHANNEL.lower(), Rewards.LIMIT_SUB + 1))


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


# --- cleanse -----------------------------------------------------------------

async def test_cleanse_lifts_a_curse_and_keeps_the_roll(db, top):
    await save_roll(S, 'victim', 90, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'actor', 'victim')).ok
    cleansed = await redeem(game.Action.CLEANSE, 'friend', '@Victim')
    assert cleansed.ok and cleansed.target == 'victim'
    assert cleansed.ceiling == Rewards.CURSE_CEILING - Rewards.CURSE_STEP
    assert cleansed.protect_minutes_left == Rewards.CLEANSE_PROTECT_MINUTES
    row = await get_roll(S, 'victim')
    assert row.curse_ceiling is None and row.value == Rewards.CURSE_CEILING
    assert (await game.free_throw(S, 'victim', limit=3)).ceiling is None


async def test_cleanse_yourself_is_allowed(db):
    await save_roll(S, 'victim', 90, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'actor', 'victim')).ok
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).ok


async def test_cleanse_without_a_curse_is_refused(db):
    await save_roll(S, 'victim', 90, free_throw=True)
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).status == game.Status.NOT_CURSED
    assert (await redeem(game.Action.CLEANSE, 'a', 'x y')).status == game.Status.EXTRA_WORDS


async def test_cleanse_cancels_the_loser_curse_still_waiting(db):
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    await save_chat_message(S, 'loser', 'привет')
    assert (await redeem(game.Action.CLEANSE, 'loser', 'loser')).ok
    assert (await game.free_throw(S, 'loser', limit=3)).ceiling is None


async def test_a_loser_curse_cleansed_before_the_first_message_is_not_announced(db):
    """The cleanse consumed the waiting curse: started anyway, the loser's first message
    would announce «проклятие залупы на 30 мин» for a curse that is never laid."""
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    assert (await redeem(game.Action.CLEANSE, 'friend', 'loser')).ok
    assert await game.appear(S, 'loser') == []
    assert 'loser' not in await get_pending_perk_users(S)


async def test_a_cleansed_player_cannot_be_cursed_for_a_while(db):
    await save_roll(S, 'victim', 90, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'a', 'victim')).ok
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).ok
    again = await redeem(game.Action.CURSE, 'b', 'victim')
    assert again.status == game.Status.CLEANSED
    assert again.protect_minutes_left == Rewards.CLEANSE_PROTECT_MINUTES
    db_ = await get_db()
    await db_.execute(
        "UPDATE roll_actions SET created_at = datetime('now', ?) WHERE action = 'cleanse'",
        (f'-{Rewards.CLEANSE_PROTECT_MINUTES} minutes',),
    )
    await db_.commit()
    assert (await redeem(game.Action.CURSE, 'b', 'victim')).ok


# --- journal -----------------------------------------------------------------

async def test_duplicate_redemption_changes_nothing(db, fixed, monkeypatch):
    await save_roll(S, 'victim', 90, free_throw=True)
    fixed.append(40)
    first = await game.redeem(game.Action.REROLL, S, 'actor', 'victim', 'same-id')
    assert first.ok
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: pytest.fail('a duplicate must not throw'))
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


async def test_rollstat_shows_the_champions_shield_once_it_runs(db):
    """The champion who has shown up sees how long the previous stream's shield still holds."""
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    await game.appear(S, 'champ')
    message = make_message('!rollstat', make_chatter('champ'))
    await handle_rollstat(CommandContext(message=message, user='champ', prompt='!rollstat',
                                         original_text='!rollstat', session_id=S, bot=FakeBot(session_id=S)))
    assert (await game.status(S, 'champ', limit=3)).shield_minutes_left == Roll.PERK_MINUTES
    assert 'texts.rollstat_perk_shield' in message.respond.await_args.args[0]


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
    ('@Nick', 'nick'), ('  @Nick,  ', 'nick'), ('Nick!', 'nick'),
    ('nick, и ещё', None), ('nick1 nick2', None), ('ник', None), ('', None), ('x' * 26, None),
])
def test_parse_nick(raw, nick):
    assert rules.parse_nick(raw) == nick


def test_minutes_left_take_the_earlier_deadline():
    """The previous stream's loser curse also ends at curse_until: announcing the floor's
    full hold would promise a lift time the curse never reaches."""
    now = 1_000_000.0
    hold = Rewards.CURSE_HOLD_MINUTES
    assert rules.minutes_left(now - 60, None, now) == hold - 1
    assert rules.minutes_left(now - 60, now + 5 * 60, now) == 5
    assert rules.minutes_left(now - 60, now + 3600, now) == hold - 1
    assert rules.minutes_left(None, now + 5 * 60, now) is None


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


@pytest.mark.parametrize(('seconds', 'minutes'), [(1, 1), (60, 1), (61, 2), (0, None), (-5, None)])
def test_whole_minutes_round_up_to_the_end(seconds, minutes):
    assert rules.whole_minutes(seconds) == minutes


def test_champion_is_hidden_when_it_is_the_loser():
    assert rules.visible_champion(('a', 1), ('a', 1)) is None
    assert rules.visible_champion(('a', 1), ('b', 99)) == ('b', 99)
    assert rules.visible_champion(None, ('b', 99)) == ('b', 99)


async def test_a_redemption_that_fails_to_journal_leaves_the_roll_as_it_was(db, monkeypatch):
    """The throw and its journal row are one transaction: a reroll that could not be
    recorded must not stay applied, or the retry after a restart would throw again."""
    await save_roll(S, 'victim', 90, free_throw=True)
    await save_chat_message(S, 'victim', 'привет')

    async def broken(*args):
        raise RuntimeError('db locked')
    monkeypatch.setattr(game, 'save_action', broken)
    with pytest.raises(RuntimeError):
        await game.redeem(game.Action.REROLL, S, 'gop', 'victim', 'r1')
    assert (await get_roll(S, 'victim')).value == 90


# --- !roll in chat -----------------------------------------------------------------

async def test_roll_after_a_burst_says_chill_once(db):
    """A subscriber has no cooldown: without the brake every further !roll would get
    its own «зачилься»."""
    bot = FakeBot(session_id=S)
    chatter = make_chatter('sub', subscriber=True)
    messages = []
    for _ in range(Roll.BURST_THROWS + 3):
        message = make_message('!roll', chatter)
        messages.append(message)
        await handle_roll(CommandContext(message=message, user='sub', prompt='!roll', original_text='!roll',
                                         session_id=S, bot=bot))
    refusals = [m.respond.await_args.args[0] for m in messages[Roll.BURST_THROWS:] if m.respond.await_args]
    assert refusals == ['texts.roll_too_fast']
    assert (await get_roll(S, 'sub')).free_throws == Roll.BURST_THROWS


async def test_roll_is_silent_for_the_whole_pause_not_just_30_seconds(db, monkeypatch):
    """The refusal is said once per pause: a second «зачилься» half a minute later
    would only feed the spam."""
    bot = FakeBot(session_id=S)
    chatter = make_chatter('sub', subscriber=True)

    async def roll():
        message = make_message('!roll', chatter)
        await handle_roll(CommandContext(message=message, user='sub', prompt='!roll', original_text='!roll',
                                         session_id=S, bot=bot))
        return message.respond.await_args.args[0] if message.respond.await_args else None

    start = time.time()
    for _ in range(Roll.BURST_THROWS):
        await roll()
    assert await roll() == 'texts.roll_too_fast'
    pause = Roll.BURST_PAUSE_MINUTES * 60
    # Past the 30-second refusal brake, still inside the pause: silence
    monkeypatch.setattr(time, 'time', lambda: start + pause - 5)
    assert await roll() is None
    # The pause is over: the throw goes through
    monkeypatch.setattr(time, 'time', lambda: start + pause + 1)
    assert await roll() not in (None, 'texts.roll_too_fast')


async def test_the_status_of_a_chat_roll_reaches_the_reroll_limit(db):
    """Only a !roll from chat sees badges: if the handler stopped passing them, every
    buyer would count as a follower."""
    chatter = make_chatter('buyer', subscriber=True)
    await handle_roll(CommandContext(message=make_message('!roll', chatter), user='buyer', prompt='!roll',
                                     original_text='!roll', session_id=S, bot=FakeBot(session_id=S)))
    *done, last = await _rerolls('buyer', Rewards.LIMIT_SUB + 1)
    assert all(outcome.ok for outcome in done)
    assert last.status == game.Status.REROLL_LIMIT


async def test_a_zero_reward_limit_means_no_limit(db, monkeypatch):
    """0 is «no limit», as for every other limit of the bot, not a fallback to the default."""
    monkeypatch.setattr(Rewards, 'LIMIT_FOLLOWER', 0)
    assert all(outcome.ok for outcome in await _rerolls('buyer', 12))
    for _ in range(12):
        assert (await redeem(game.Action.SHIELD, 'buyer')).ok
        await _expire_shields()


def test_a_burst_pause_is_one_minute_by_default():
    assert Roll.BURST_PAUSE_MINUTES == 1
