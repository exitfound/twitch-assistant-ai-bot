"""The «залупа стрима» game on a temporary database: throws, rewards, curses, perks.

Channel points depend on this module, so every documented rule has a test here.
"""
import asyncio
import time

import pytest

from fakes import FakeBot, always, make_chatter, make_message
from src.twitch.core.commands import CommandContext
from src.core.config import Follow, Rewards, Roll, Twitch
from src.twitch.core.component import ChatComponent
from src.twitch.core.viewer import Tier
from src.core.database import get_db, save_chat_message, save_stream
from src.twitch.local.roll import game, perks, rules
from src.twitch.local.roll import command as command_module
from src.twitch.local.roll.command import handle_roll, handle_rollstat
from src.twitch.local.roll.texts import compose
from src.twitch.local.roll.storage import (
    RollRow, add_choke, add_perk, get_due_chokes, get_pending_perk_users, get_perk, get_roll, get_session_champion,
    count_actions, get_session_loser, save_roll, save_tier, set_curse,
)

S = '2026-09-22 20:00'
_ids = iter(range(1, 10_000))


def redeem(action: str, actor: str, user_input: str = '', session: str = S, **kwargs):
    return game.redeem(action, session, actor, user_input, f'redemption-{next(_ids)}', **kwargs)


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


async def test_extra_uses_the_limit_of_the_status_from_chat(db):
    """A redemption carries no badges: a subscriber's limit comes from the status chat
    showed, even before their first throw."""
    await save_tier(S, 'sub', Tier.SUB)
    outcome = await redeem(game.Action.EXTRA, 'sub')
    assert outcome.status == game.Status.FREE_LEFT
    assert outcome.free_left == Roll.FREE_SUB
    await game.free_throw(S, 'sub', limit=Roll.FREE_SUB)
    assert (await redeem(game.Action.EXTRA, 'sub')).free_left == Roll.FREE_SUB - 1


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
    assert refused.wait_minutes == Roll.BURST_PAUSE_MINUTES


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


async def test_a_lone_player_is_the_champion(db, fixed):
    """Whoever plays alone wins by default: there is nobody to be the loser against."""
    fixed += [5]
    outcome = await game.free_throw(S, 'alone', limit=3)
    assert outcome.champion == ('alone', 5)
    assert outcome.loser is None
    standing = await game.status(S, 'alone', limit=3)
    assert (standing.champion, standing.loser) == (('alone', 5), None)


async def test_nobody_holds_both_titles_when_all_rolls_are_equal(db, fixed):
    """Matching the loser's minimum makes you the loser, and the champion is the best of the rest."""
    fixed += [50, 50, 50]
    await game.free_throw(S, 'a', limit=3)
    outcome = await game.free_throw(S, 'b', limit=3)
    assert outcome.loser == ('b', 50)
    assert outcome.champion == ('a', 50)
    outcome = await game.free_throw(S, 'c', limit=3)
    assert (outcome.loser, outcome.champion) == (('c', 50), ('b', 50))


async def test_equal_rolls_go_to_whoever_threw_last(db, fixed):
    """Matching the loser makes you the loser, matching the best makes you the best."""
    fixed += [50, 9, 9, 90, 90]
    await game.free_throw(S, 'x', limit=3)
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
    row = RollRow(value=30, free_throws=1, curse_ceiling=25, curse_floor_at=None, curse_until=None)
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
    (None, Rewards.LIMIT_FOLLOWER),            # never wrote in this stream: a follower
    (Tier.REGULAR, Rewards.LIMIT_FOLLOWER),
    (Tier.VIP, Rewards.LIMIT_VIP),
    (Tier.SUB, Rewards.LIMIT_SUB),
    (Tier.MODERATOR, Rewards.LIMIT_MOD),
])
async def test_rerolls_per_window_follow_the_status_from_chat(db, tier, limit):
    if tier is not None:
        await save_tier(S, 'buyer', tier)
    *done, refused = await _rerolls('buyer', limit + 1)
    assert all(outcome.ok for outcome in done)
    assert refused.status == game.Status.REROLL_LIMIT
    assert refused.limit == limit
    assert refused.wait_minutes == Rewards.REROLL_WINDOW_MINUTES


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


async def test_a_status_gained_mid_stream_counts_from_the_next_message(db):
    await save_tier(S, 'buyer', Tier.REGULAR)
    await save_tier(S, 'buyer', Tier.SUB)
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
    assert again.guard_minutes == Rewards.SHIELD_MINUTES
    blocked = await redeem(game.Action.REROLL, 'actor', 'victim')
    assert blocked.status == game.Status.SHIELDED
    assert blocked.guard_minutes == Rewards.SHIELD_MINUTES


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


async def test_shield_runs_on_the_bots_clock(db, monkeypatch):
    """The shield, the respite after a reroll and the ward after a cleanse count time by
    time.time(), like curses and perks: one clock for every rule of the game."""
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.SHIELD, 'victim')).ok
    start = time.time()
    monkeypatch.setattr(time, 'time', lambda: start + (Rewards.SHIELD_MINUTES - 5) * 60)
    assert (await game.status(S, 'victim', limit=3)).shield_left == 5
    monkeypatch.setattr(time, 'time', lambda: start + Rewards.SHIELD_MINUTES * 60 + 1)
    assert (await game.status(S, 'victim', limit=3)).shield_left is None
    assert (await redeem(game.Action.REROLL, 'actor', 'victim')).ok


async def test_a_stream_start_drops_the_curses_of_finished_streams(db):
    """A curse that never reached the floor stays set in its session: the lift checks only
    the current one. The next stream clears it silently and keeps its own session's."""
    for session in ('P', S):
        await save_roll(session, 'victim', 50, free_throw=True)
        await set_curse(session, 'victim', 55, None)
    assert await game.drop_finished_curses(S) == 1
    assert (await get_roll('P', 'victim')).curse_ceiling is None
    assert (await get_roll(S, 'victim')).curse_ceiling == 55
    assert await game.drop_finished_curses(S) == 0


async def test_a_stream_start_drops_the_chokes_of_finished_streams(db):
    """A stream that ended mid-choke leaves its row; the current session's choke runs on."""
    for session in ('P', S):
        await add_choke(session, 'victim', 1.0, 2.0)
    await game.drop_finished_curses(S)
    assert await get_due_chokes('P', 10.0) == []
    assert len(await get_due_chokes(S, 10.0)) == 1


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
async def test_shields_per_stream_follow_the_status_from_chat(db, tier, limit):
    """The limit is per stream: once used up, an expired shield cannot be bought again."""
    if tier is not None:
        await save_tier(S, 'buyer', tier)
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
    assert refused.guard_minutes == Roll.PERK_MINUTES


async def _curses(actor: str, count: int) -> list[game.Outcome]:
    return [await redeem(game.Action.CURSE, actor, target) for target in await _targets(count)]


@pytest.mark.parametrize(('tier', 'limit'), [
    (None, Rewards.LIMIT_FOLLOWER),
    (Tier.VIP, Rewards.LIMIT_VIP),
    (Tier.SUB, Rewards.LIMIT_SUB),
])
async def test_curses_per_stream_follow_the_status_from_chat(db, tier, limit):
    if tier is not None:
        await save_tier(S, 'buyer', tier)
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
async def test_cleanses_per_stream_follow_the_status_from_chat(db, tier, limit):
    if tier is not None:
        await save_tier(S, 'buyer', tier)
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
    assert outcome.guard_minutes == Roll.PERK_MINUTES


async def test_protection_after_a_reroll(db):
    await save_roll(S, 'victim', 50, free_throw=True)
    assert (await redeem(game.Action.REROLL, 'a', 'victim')).ok
    second = await redeem(game.Action.REROLL, 'b', 'victim')
    assert second.status == game.Status.PROTECTED
    assert second.guard_minutes == Rewards.REROLL_PROTECT_MINUTES


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

async def test_cleanse_lifts_a_curse_and_returns_the_roll_it_threw_over(db, top):
    await save_roll(S, 'victim', 90, free_throw=True)
    await save_roll(S, 'other', 80, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'actor', 'victim')).ok
    cleansed = await redeem(game.Action.CLEANSE, 'friend', '@Victim')
    assert cleansed.ok and cleansed.target == 'victim'
    assert (cleansed.old_value, cleansed.value) == (Rewards.CURSE_CEILING, 90)
    assert cleansed.ceiling == Rewards.CURSE_CEILING - Rewards.CURSE_STEP
    assert cleansed.guard_minutes == Rewards.CLEANSE_PROTECT_MINUTES
    # The returned roll changes the titles: the victim is the champion again
    assert cleansed.loser == ('other', 80) and cleansed.champion == ('victim', 90)
    row = await get_roll(S, 'victim')
    assert row.curse_ceiling is None and row.value == 90
    assert (await game.free_throw(S, 'victim', limit=3)).ceiling is None


async def test_a_returned_roll_loses_ties(db, fixed):
    """A returned number is not a fresh throw: on a tie the real throw keeps its title."""
    fixed.extend([30, 5])
    await save_roll(S, 'victim', 90, free_throw=True)
    await save_roll(S, 'champ', 90, free_throw=True)
    await save_roll(S, 'low', 20, free_throw=True)
    await save_roll(S, 'loser', 20, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'a', 'victim')).value == 30
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).champion == ('champ', 90)
    assert (await redeem(game.Action.CURSE, 'b', 'low')).value == 5
    assert (await redeem(game.Action.CLEANSE, 'low', 'low')).loser == ('loser', 20)


async def test_cleanse_keeps_a_roll_higher_than_the_one_before_the_curse(db, fixed):
    """A throw under the curse beat the roll the curse threw over: it stays."""
    fixed.extend([20, 70])
    await save_roll(S, 'victim', 40, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'actor', 'victim')).value == 20
    assert (await game.free_throw(S, 'victim', limit=3)).value == 70
    cleansed = await redeem(game.Action.CLEANSE, 'friend', 'victim')
    assert cleansed.ok and cleansed.value is None
    assert (await get_roll(S, 'victim')).value == 70


async def test_cleanse_returns_the_roll_before_the_latest_curse(db, fixed):
    """Two curses in a stream: the second threw over the roll left after the first."""
    fixed.extend([30, 50, 10])
    await save_roll(S, 'victim', 90, free_throw=True)
    assert (await redeem(game.Action.CURSE, 'a', 'victim')).value == 30
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).value == 90
    db_ = await get_db()
    await db_.execute(
        "UPDATE roll_actions SET created_at = datetime('now', ?) WHERE action = 'cleanse'",
        (f'-{Rewards.CLEANSE_PROTECT_MINUTES} minutes',),
    )
    await db_.commit()
    assert (await game.free_throw(S, 'victim', limit=3)).value == 50
    assert (await redeem(game.Action.CURSE, 'b', 'victim')).value == 10
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).value == 50


async def test_cleanse_of_the_loser_curse_returns_nothing(db, fixed):
    """The previous stream's curse fell on a first throw: there was no roll before it."""
    fixed.append(5)
    await _previous_stream({'champ': 95, 'loser': 3})
    await game.grant_perks(S)
    assert (await game.free_throw(S, 'loser', limit=3)).ceiling == Rewards.CURSE_CEILING
    cleansed = await redeem(game.Action.CLEANSE, 'friend', 'loser')
    assert cleansed.ok and cleansed.value is None
    assert (await get_roll(S, 'loser')).value == 5


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
    assert again.guard_minutes == Rewards.CLEANSE_PROTECT_MINUTES
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


async def test_without_rewards_the_champion_waits_for_the_shield(db):
    """The Благодать guards only against rerolls: while the rewards are down it is held
    back, and granted once they come up, without granting the curse twice."""
    await _previous_stream({'champ': 95, 'loser': 3})
    assert await game.grant_perks(S, shield=False) == (None, 'loser')
    assert await game.grant_perks(S) == ('champ', None)
    assert await game.grant_perks(S) is None


async def test_the_stream_start_holds_the_shield_back_without_rewards(db):
    await _previous_stream({'champ': 95, 'loser': 3})
    bot = FakeBot(session_id=S)
    await perks.on_stream_start(bot, S)
    bot.send_chat_message.assert_awaited_once_with('texts.roll_perks_loser')
    bot.rewards_active = True
    await perks.on_stream_start(bot, S)
    assert bot.send_chat_message.await_args.args[0] == 'texts.roll_perks_champion'


async def test_a_lone_player_gets_only_the_blessing(db):
    """Alone in the previous stream, they were its champion, not its loser."""
    await _previous_stream({'alone': 50})
    assert await game.grant_perks(S) == ('alone', None)


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


async def test_a_redemption_that_fails_to_journal_leaves_the_roll_as_it_was(db, monkeypatch):
    """The throw and its journal row are one transaction: a reroll that could not be
    recorded must not stay applied, or the retry after a restart would throw again."""
    await save_roll(S, 'victim', 90, free_throw=True)
    await save_chat_message(S, 'victim', 'привет')

    async def broken(*args, **kwargs):
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


async def test_no_free_throws_is_said_once_per_30_seconds(db, monkeypatch):
    """A subscriber has no cooldown: past the free throws every further !roll would get
    its own refusal. Like every limit refusal, it repeats at most once per 30 s."""
    bot = FakeBot(session_id=S)
    chatter = make_chatter('sub', subscriber=True)

    async def roll():
        message = make_message('!roll', chatter)
        await handle_roll(CommandContext(message=message, user='sub', prompt='!roll', original_text='!roll',
                                         session_id=S, bot=bot))
        return message.respond.await_args.args[0] if message.respond.await_args else None

    monkeypatch.setattr(Roll, 'FREE_SUB', 2)
    await roll()
    await roll()
    assert (await roll()).startswith('texts.roll_no_free')
    assert await roll() is None
    start = time.time()
    monkeypatch.setattr(time, 'time', lambda: start + 31)
    assert (await roll()).startswith('texts.roll_no_free')
    assert (await get_roll(S, 'sub')).free_throws == 2


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


async def test_the_status_from_chat_reaches_the_reroll_limit(db, monkeypatch):
    """Only a chat message carries badges: if the dispatcher stopped storing them, every
    buyer would count as a follower."""
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    await ChatComponent(FakeBot(session_id=S)).event_message(
        make_message('всем привет', make_chatter('buyer', subscriber=True)))
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


# --- flasks -----------------------------------------------------------------------

async def _players(**rolls: int) -> None:
    for user, value in rolls.items():
        await save_roll(S, user, value, free_throw=True)


async def test_a_swap_trades_the_buyers_roll_for_a_partners(db, swap):
    await _players(buyer=10, partner=90)
    outcome = await redeem(game.Action.FLASK, 'buyer')
    assert outcome.ok and outcome.flask == game.Flask.SWAP
    assert (outcome.target, outcome.old_value, outcome.value) == ('partner', 10, 90)
    assert outcome.guard_minutes == Rewards.FLASK_MINUTES
    assert (await get_roll(S, 'buyer')).value == 90
    assert (await get_roll(S, 'partner')).value == 10
    assert outcome.champion == ('buyer', 90) and outcome.loser == ('partner', 10)


async def test_a_swap_keeps_the_free_throws_and_the_curse_with_the_player(db, swap):
    await _players(buyer=10, partner=90)
    await set_curse(S, 'partner', 55, None)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    partner = await get_roll(S, 'partner')
    assert (partner.value, partner.free_throws, partner.curse_ceiling) == (10, 1, 55)
    assert (await get_roll(S, 'buyer')).curse_ceiling is None


async def test_a_swap_draws_its_partner_at_random(db, swap, monkeypatch):
    await _players(buyer=10, a=20, b=30, c=40)
    seen = []
    monkeypatch.setattr(game.random, 'choice', lambda seq: seen.append(list(seq)) or seq[-1])
    outcome = await redeem(game.Action.FLASK, 'buyer')
    assert seen == [[('a', 20), ('b', 30), ('c', 40)]]
    assert outcome.target == 'c'


async def test_a_swap_needs_the_buyers_own_roll(db, swap):
    await _players(partner=90)
    refused = await redeem(game.Action.FLASK, 'buyer')
    assert refused.status == game.Status.NO_ROLL
    assert (await get_roll(S, 'partner')).value == 90


async def test_a_swap_skips_the_shielded_the_blessed_and_the_equal(db, swap):
    """A bought shield and a Благодать keep their owner out of the draw, and a partner
    with the buyer's own number would change nothing."""
    await _players(buyer=10, shielded=90, blessed=80, same=10)
    assert (await redeem(game.Action.SHIELD, 'shielded')).ok
    await add_perk(S, 'blessed', game.Perk.SHIELD, 'previous')
    refused = await redeem(game.Action.FLASK, 'buyer')
    assert refused.status == game.Status.NO_PARTNER
    await game.appear(S, 'blessed')
    assert (await redeem(game.Action.FLASK, 'buyer')).status == game.Status.NO_PARTNER
    assert (await get_perk(S, 'blessed', game.Perk.SHIELD)).active_until is not None


async def test_a_swap_skips_players_another_swap_holds(db, swap):
    await _players(first=10, second=90, third=50)
    first = await redeem(game.Action.FLASK, 'first')
    assert first.ok
    other = 'second' if first.target == 'third' else 'third'
    # The pair is held: the third player has nobody left to swap with
    assert (await redeem(game.Action.FLASK, other)).status == game.Status.NO_PARTNER


async def test_the_swap_holds_both_players_off_their_own_throws(db, swap):
    await _players(buyer=10, partner=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    for user in ('buyer', 'partner'):
        refused = await game.free_throw(S, user, limit=3)
        assert refused.status == game.Status.UNDER_FLASK
        assert refused.wait_minutes == Rewards.FLASK_MINUTES
        assert (await get_roll(S, user)).free_throws == 1
    # A paid throw is checked past the free ones: they are used up first
    for _ in range(Roll.FREE_PER_SESSION - 1):
        await save_roll(S, 'partner', 10, free_throw=True)
    assert (await redeem(game.Action.EXTRA, 'partner')).status == game.Status.UNDER_FLASK


async def test_the_swap_holds_both_players_out_of_rerolls_both_ways(db, swap):
    await _players(buyer=10, partner=90, other=50)
    await redeem(game.Action.SHIELD, 'other')
    assert (await redeem(game.Action.FLASK, 'buyer')).target == 'partner'
    refused = await redeem(game.Action.REROLL, 'buyer', 'other')
    assert refused.status == game.Status.UNDER_FLASK
    for target in ('buyer', 'partner'):
        guarded = await redeem(game.Action.REROLL, 'other', target)
        assert guarded.status == game.Status.FLASK_GUARDED
        assert guarded.guard_minutes == Rewards.FLASK_MINUTES
    assert (await get_roll(S, 'buyer')).value == 90


async def test_a_curse_still_lands_on_a_swapped_player(db, swap, top):
    await _players(buyer=10, partner=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    cursed = await redeem(game.Action.CURSE, 'foe', 'buyer')
    assert cursed.ok and cursed.value == Rewards.CURSE_CEILING


async def test_the_held_players_still_shield_curse_and_cleanse(db, swap):
    await _players(buyer=10, partner=90, other=50)
    assert (await redeem(game.Action.SHIELD, 'other')).ok
    assert (await redeem(game.Action.FLASK, 'buyer')).target == 'partner'
    assert (await redeem(game.Action.SHIELD, 'buyer')).ok
    assert (await redeem(game.Action.CURSE, 'partner', 'other')).ok
    assert (await redeem(game.Action.CLEANSE, 'buyer', 'other')).ok


async def test_the_swap_lets_go_after_its_minutes(db, swap, monkeypatch):
    await _players(buyer=10, partner=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    start = time.time()
    monkeypatch.setattr(time, 'time', lambda: start + (Rewards.FLASK_MINUTES - 5) * 60)
    assert (await game.status(S, 'partner', limit=3)).flask_left == 5
    monkeypatch.setattr(time, 'time', lambda: start + Rewards.FLASK_MINUTES * 60 + 1)
    assert (await game.status(S, 'partner', limit=3)).flask_left is None
    assert (await game.free_throw(S, 'partner', limit=3)).ok
    assert (await redeem(game.Action.REROLL, 'partner', 'buyer')).ok


async def test_flasks_per_stream_follow_the_status_from_chat(db, swap, monkeypatch):
    await _players(buyer=10, partner=90)
    for _ in range(Rewards.LIMIT_FOLLOWER):
        assert (await redeem(game.Action.FLASK, 'buyer')).ok
        db_ = await get_db()
        await db_.execute("UPDATE roll_actions SET acted_at = acted_at - ? WHERE action = 'flask'",
                          (Rewards.FLASK_MINUTES * 60,))
        await db_.commit()
    refused = await redeem(game.Action.FLASK, 'buyer')
    assert refused.status == game.Status.FLASK_LIMIT
    assert refused.limit == Rewards.LIMIT_FOLLOWER


async def test_a_held_roll_is_refused_once_per_30_seconds(db, swap):
    await _players(sub=10, partner=90)
    assert (await redeem(game.Action.FLASK, 'sub')).ok
    bot = FakeBot(session_id=S)

    async def roll():
        message = make_message('!roll', make_chatter('sub', subscriber=True))
        await handle_roll(CommandContext(message=message, user='sub', prompt='!roll', original_text='!roll',
                                         session_id=S, bot=bot))
        return message.respond.await_args.args[0] if message.respond.await_args else None

    assert await roll() == 'texts.roll_under_flask'
    assert await roll() is None
    assert (await get_roll(S, 'sub')).value == 90


async def test_rollstat_shows_the_swaps_minutes(db, swap):
    await _players(buyer=10, partner=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    message = make_message('!rollstat', make_chatter('partner'))
    await handle_rollstat(CommandContext(message=message, user='partner', prompt='!rollstat',
                                         original_text='!rollstat', session_id=S, bot=FakeBot(session_id=S)))
    assert 'texts.rollstat_flask' in message.respond.await_args.args[0]



async def test_a_held_buyer_swaps_again_and_is_held_longer(db, swap):
    await _players(buyer=10, first=90, second=50, third=70)
    first = await redeem(game.Action.FLASK, 'buyer')
    assert first.ok and first.extra is None
    again = await redeem(game.Action.FLASK, 'buyer')
    assert again.ok and again.target != first.target and again.extra == Rewards.SWAP_EXTRA_MINUTES
    assert (await game.status(S, 'buyer', limit=3)).flask_left == Rewards.FLASK_MINUTES + Rewards.SWAP_EXTRA_MINUTES
    assert (await game.status(S, again.target, limit=3)).flask_left == Rewards.FLASK_MINUTES

# --- the luck flask -------------------------------------------------------------

@pytest.fixture
def unlucky(monkeypatch):
    """No throw hits the top by chance; the plain throws record their range."""
    ranges: list[tuple[int, int]] = []
    monkeypatch.setattr(rules.random, 'random', lambda: 0.999)
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: ranges.append((a, b)) or a)
    return ranges


def _percent(throw_no: int) -> int:
    return Rewards.LUCK_STEP * throw_no


async def test_the_luck_flask_is_journaled_and_changes_no_roll(db, luck):
    await _players(buyer=10)
    outcome = await redeem(game.Action.FLASK, 'buyer')
    assert outcome.ok and outcome.flask == game.Flask.LUCK
    assert (outcome.target, outcome.value, outcome.luck) == ('buyer', None, _percent(1))
    assert outcome.guard_minutes == Rewards.FLASK_MINUTES
    assert (await get_roll(S, 'buyer')).value == 10


async def test_own_throws_under_luck_go_higher_and_hit_the_top_more_often(db, luck, unlucky):
    await _players(buyer=10)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    chances = []
    # The broadcaster's throws: no spam brake between them
    for _ in range(Rewards.LUCK_THROWS + 2):
        outcome = await game.free_throw(S, 'buyer', limit=100, unlimited=True)
        chances.append(outcome.luck)
    assert set(unlucky) == {(Rewards.LUCK_FLOOR, Roll.MAX - 1)}
    assert chances == [_percent(n) for n in range(1, Rewards.LUCK_THROWS + 1)] + [_percent(Rewards.LUCK_THROWS)] * 2
    assert _percent(Rewards.LUCK_THROWS) == 20


async def test_a_throw_just_before_the_luck_does_not_count_as_lucky(db, luck, unlucky, monkeypatch):
    """The drink's moment is kept to the fraction of a second: a throw a moment earlier, in the
    same second, is not the first lucky one."""
    await _players(buyer=10)
    start = float(int(time.time())) + 0.3
    monkeypatch.setattr(time, 'time', lambda: start)
    await game.free_throw(S, 'buyer', limit=3)
    monkeypatch.setattr(time, 'time', lambda: start + 0.5)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    monkeypatch.setattr(time, 'time', lambda: start + 0.6)
    assert (await game.free_throw(S, 'buyer', limit=3)).luck == _percent(1)


async def test_a_lucky_throw_hits_the_top_with_its_chance(db, luck, monkeypatch):
    await _players(buyer=10)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    monkeypatch.setattr(rules.random, 'random', lambda: _percent(1) / 100 - 0.001)
    assert (await game.free_throw(S, 'buyer', limit=3)).value == Roll.MAX


async def test_an_extra_roll_under_luck_is_lucky_too(db, luck, unlucky):
    await _players(buyer=10)
    for _ in range(Roll.FREE_PER_SESSION - 1):
        await save_roll(S, 'buyer', 10, free_throw=True)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    outcome = await redeem(game.Action.EXTRA, 'buyer')
    assert outcome.ok and outcome.luck == _percent(1)
    assert unlucky == [(Rewards.LUCK_FLOOR, Roll.MAX - 1)]


async def test_a_reroll_on_the_lucky_one_is_a_plain_throw(db, luck, unlucky):
    await _players(buyer=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    outcome = await redeem(game.Action.REROLL, 'foe', 'buyer')
    assert outcome.ok and outcome.luck is None
    assert unlucky == [(Roll.MIN, Roll.MAX)]
    # The reroll is not their throw: the next own one still has the first chance
    assert (await game.free_throw(S, 'buyer', limit=3)).luck == _percent(1)


async def test_no_curse_lands_on_the_lucky_one(db, luck):
    await _players(buyer=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    refused = await redeem(game.Action.CURSE, 'foe', 'buyer')
    assert refused.status == game.Status.LUCK_GUARDED
    assert refused.guard_minutes == Rewards.FLASK_MINUTES


async def test_the_sglaz_waits_out_the_luck(db, luck, unlucky):
    await _players(loser=50)
    await add_perk(S, 'loser', game.Perk.CURSE, 'previous')
    await game.appear(S, 'loser')
    assert (await redeem(game.Action.FLASK, 'loser')).ok
    assert (await game.free_throw(S, 'loser', limit=3)).ceiling is None
    assert (await redeem(game.Action.REROLL, 'foe', 'loser')).ceiling is None
    assert not (await get_perk(S, 'loser', game.Perk.CURSE)).consumed


async def test_a_cursed_buyers_luck_waits_for_the_curse_to_lift(db, luck, top):
    """The curse beats the luck: no refund – that would be a free redraw – and no lucky throw
    until the curse is gone."""
    await _players(buyer=50)
    await set_curse(S, 'buyer', 55, None)
    outcome = await redeem(game.Action.FLASK, 'buyer')
    assert outcome.ok and outcome.ceiling == 55
    throw = await game.free_throw(S, 'buyer', limit=3)
    assert (throw.value, throw.luck) == (55, None)
    assert (await redeem(game.Action.CLEANSE, 'buyer', 'buyer')).ok
    assert (await game.free_throw(S, 'buyer', limit=3)).luck is not None


async def test_a_second_luck_stacks_its_strength_and_keeps_the_chance(db, luck, monkeypatch, clock):
    await _players(buyer=50)
    assert (await redeem(game.Action.FLASK, 'buyer')).stacks == 1
    await game.free_throw(S, 'buyer', limit=3)
    clock(5 * 60)
    second = await redeem(game.Action.FLASK, 'buyer')
    assert second.ok and second.stacks == 2
    standing = await game.status(S, 'buyer', limit=3)
    # Two stacks are twice the step, and the throw already made still counts: the second throw
    assert (standing.luck_stacks, standing.luck_next) == (2, 2 * 2 * Rewards.LUCK_STEP)
    assert standing.luck_left == Rewards.FLASK_MINUTES
    # The first stack runs out on its own minutes, the second keeps the run going
    clock(Rewards.FLASK_MINUTES * 60 + 1)
    assert (await game.status(S, 'buyer', limit=3)).luck_stacks == 1


async def test_the_luck_ends_after_its_minutes(db, luck, unlucky, monkeypatch):
    await _players(buyer=50)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    start = time.time()
    monkeypatch.setattr(time, 'time', lambda: start + Rewards.FLASK_MINUTES * 60 + 1)
    assert (await game.free_throw(S, 'buyer', limit=3)).luck is None
    assert unlucky == [(Roll.MIN, Roll.MAX)]
    assert (await redeem(game.Action.CURSE, 'foe', 'buyer')).ok


async def test_the_swap_may_draw_the_lucky_one(db, monkeypatch):
    """Only the swap's own hold and the shields keep a player out of its draw."""
    await _players(buyer=10, lucky=90)
    always(monkeypatch, game.Flask.LUCK)
    assert (await redeem(game.Action.FLASK, 'lucky')).ok
    always(monkeypatch, game.Flask.SWAP)
    assert (await redeem(game.Action.FLASK, 'buyer')).target == 'lucky'


async def test_only_rollstat_names_the_luck(db, luck):
    await _players(buyer=50)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    standing = await game.status(S, 'buyer', limit=3)
    assert (standing.luck_left, standing.luck_next) == (Rewards.FLASK_MINUTES, _percent(1))
    # A lucky !roll says nothing extra: the chance is for !rollstat, not for every throw
    message = make_message('!roll', make_chatter('buyer'))
    await handle_roll(CommandContext(message=message, user='buyer', prompt='!roll', original_text='!roll',
                                     session_id=S, bot=FakeBot(session_id=S)))
    assert 'luck' not in message.respond.await_args.args[0]
    message = make_message('!rollstat', make_chatter('buyer'))
    await handle_rollstat(CommandContext(message=message, user='buyer', prompt='!rollstat',
                                         original_text='!rollstat', session_id=S, bot=FakeBot(session_id=S)))
    assert 'texts.rollstat_luck' in message.respond.await_args.args[0]



async def test_a_long_rollstat_goes_out_in_pieces(db, luck, monkeypatch):
    """Many effects at once can pass Twitch's limit: the answer is cut, not dropped."""
    from src.twitch.local.roll import texts
    monkeypatch.setattr(texts, 'MESSAGE_MAX', 40)
    await _players(buyer=50)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    message = make_message('!rollstat', make_chatter('buyer'))
    await handle_rollstat(CommandContext(message=message, user='buyer', prompt='!rollstat',
                                         original_text='!rollstat', session_id=S, bot=FakeBot(session_id=S)))
    pieces = [c.args[0] for c in message.respond.await_args_list]
    assert len(pieces) > 1 and all(len(piece) <= 40 for piece in pieces)


async def test_rollstat_names_a_doubled_curse_and_the_agony_cap(db, agony, monkeypatch):
    await _players(buyer=50, victim=90)
    await set_curse(S, 'victim', 55, None)
    monkeypatch.setattr(Rewards, 'AGONY_CURSE', 0.0)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    assert (await game.status(S, 'victim', limit=3)).curse_power == 2
    seen = {}
    real = command_module.Content.text

    def text(key, **values):
        seen[key] = values
        return real(key, **values)
    monkeypatch.setattr(command_module.Content, 'text', text)
    message = make_message('!rollstat', make_chatter('victim'))
    await handle_rollstat(CommandContext(message=message, user='victim', prompt='!rollstat',
                                         original_text='!rollstat', session_id=S, bot=FakeBot(session_id=S)))
    assert seen['rollstat_cursed']['ceiling'] == Rewards.AGONY_THROW_TOP
    assert seen['rollstat_curse_doubled'] == {'power': 2, 'step': Rewards.CURSE_STEP // 2,
                                              'hold': 2 * Rewards.CURSE_HOLD_MINUTES}
    assert 'rollstat_agony' in seen

# --- the choking flask -----------------------------------------------------------

@pytest.fixture
def choke(monkeypatch):
    """The random flask always comes up as the choking flask."""
    always(monkeypatch, game.Flask.CHOKE)


@pytest.fixture
def clock(monkeypatch):
    """The bot's clock, moved by hand: clock(seconds) jumps that far from the start."""
    start = time.time()
    monkeypatch.setattr(time, 'time', lambda: start)

    def move(seconds: float) -> None:
        monkeypatch.setattr(time, 'time', lambda: start + seconds)
    return move


async def test_the_choke_takes_a_step_every_minute_from_the_drink(db, choke, clock, fixed):
    await _players(buyer=37)
    fixed.append(88)
    outcome = await redeem(game.Action.FLASK, 'buyer')
    assert outcome.ok and outcome.flask == game.Flask.CHOKE and outcome.value == 37
    assert (await game.free_throw(S, 'buyer', limit=3)).value == 88
    clock(59)
    assert await game.tick_chokes(S) is None
    clock(3 * 60 + 1)
    standings = await game.tick_chokes(S)
    assert (await get_roll(S, 'buyer')).value == 88 - 3 * Rewards.CHOKE_STEP
    assert standings.champion == ('buyer', 88 - 3 * Rewards.CHOKE_STEP)
    # A new throw is worn down from the next minute of the schedule, not a minute after it
    fixed.append(100)
    clock(3 * 60 + 50)
    assert (await game.free_throw(S, 'buyer', limit=3)).value == 100
    clock(4 * 60)
    await game.tick_chokes(S)
    assert (await get_roll(S, 'buyer')).value == 100 - Rewards.CHOKE_STEP


async def test_the_choke_never_goes_below_the_minimum_and_ends(db, choke, clock):
    await _players(buyer=25)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    clock(Rewards.FLASK_MINUTES * 60 + 300)
    await game.tick_chokes(S)
    assert (await get_roll(S, 'buyer')).value == Roll.MIN
    await save_roll(S, 'buyer', 80, free_throw=False)
    assert await game.tick_chokes(S) is None
    assert (await get_roll(S, 'buyer')).value == 80
    assert (await game.status(S, 'buyer', limit=3)).choke_left is None


async def test_the_chokes_minutes_follow_its_schedule_not_the_setting(db, choke, monkeypatch):
    """A changed REWARD_FLASK_MINUTES does not cut a running choke short in !rollstat: it reads
    the schedule the steps follow."""
    await _players(buyer=50)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    monkeypatch.setattr(Rewards, 'FLASK_MINUTES', 1)
    assert (await game.status(S, 'buyer', limit=3)).choke_left == 15


async def test_the_choke_takes_exactly_its_minutes_of_steps(db, choke, clock):
    await _players(buyer=100)
    await save_roll(S, 'buyer', 1000, free_throw=False)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    for minute in range(1, Rewards.FLASK_MINUTES + 3):
        clock(minute * 60)
        await game.tick_chokes(S)
    assert (await get_roll(S, 'buyer')).value == 1000 - Rewards.FLASK_MINUTES * Rewards.CHOKE_STEP


@pytest.mark.parametrize('flask', list(game.Flask))
async def test_every_flask_needs_the_buyers_own_roll_before_the_draw(db, monkeypatch, flask):
    """A refund after the draw would let a buyer with no roll draw again for free until a
    flask that needs no roll came up."""
    drawn = []
    monkeypatch.setattr(game.random, 'choice', lambda seq: drawn.append(seq) or flask)
    refused = await redeem(game.Action.FLASK, 'buyer')
    assert refused.status == game.Status.NO_ROLL and refused.flask is None
    assert drawn == [] and await game.tick_chokes(S) is None


async def test_two_chokes_step_together_each_on_its_own_minutes(db, choke, clock):
    await _players(buyer=90)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    clock(30)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    assert (await game.status(S, 'buyer', limit=3)).choke_stacks == 2
    assert (await redeem(game.Action.CLEANSE, 'buyer', 'buyer')).status == game.Status.NOT_CURSED
    clock(90)
    await game.tick_chokes(S)
    assert (await get_roll(S, 'buyer')).value == 90 - 2 * Rewards.CHOKE_STEP
    # The first one ends first: from then on one step a minute
    clock(Rewards.FLASK_MINUTES * 60 + 1)
    await game.tick_chokes(S)
    assert (await game.status(S, 'buyer', limit=3)).choke_stacks == 1


async def test_the_choke_stays_with_the_player_through_a_swap(db, monkeypatch, clock):
    await _players(choked=90, other=20)
    always(monkeypatch, game.Flask.CHOKE)
    assert (await redeem(game.Action.FLASK, 'choked')).ok
    always(monkeypatch, game.Flask.SWAP)
    assert (await redeem(game.Action.FLASK, 'other')).target == 'choked'
    clock(60)
    await game.tick_chokes(S)
    assert (await get_roll(S, 'choked')).value == 20 - Rewards.CHOKE_STEP
    assert (await get_roll(S, 'other')).value == 90


# --- the mass flasks ---------------------------------------------------------------

@pytest.fixture
def agony(monkeypatch):
    """The random flask always comes up as АГОНИЯ ЧЕРНОМИРЬЯ, and its curse always lands."""
    always(monkeypatch, game.Flask.AGONY)
    monkeypatch.setattr(Rewards, 'AGONY_CURSE', 1.0)


@pytest.fixture
def blessing(monkeypatch):
    """The random flask always comes up as БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА."""
    always(monkeypatch, game.Flask.BLESSING)


async def test_the_flasks_are_drawn_by_their_weights(db, monkeypatch):
    await _players(buyer=50, other=60)
    seen = []

    def choices(population, weights):
        seen.append(dict(zip(population, weights, strict=True)))
        return [game.Flask.CHOKE]
    monkeypatch.setattr(game.random, 'choices', choices)
    assert (await redeem(game.Action.FLASK, 'buyer')).flask == game.Flask.CHOKE
    assert seen == [{game.Flask.SWAP: 30, game.Flask.LUCK: 30, game.Flask.CHOKE: 30,
                     game.Flask.AGONY: 5, game.Flask.BLESSING: 5}]


async def test_agony_reaches_everyone_in_chat_but_the_bots(db, agony):
    await _players(buyer=50, player=90, gone=70)
    outcome = await redeem(game.Action.FLASK, 'buyer', chatters={'player', 'lurker', 'nightbot'})
    assert outcome.ok and outcome.flask == game.Flask.AGONY
    assert (outcome.crowd, outcome.cursed) == (4, 4)
    for name in ('buyer', 'player', 'gone', 'lurker'):
        row = await get_roll(S, name)
        assert Roll.MIN <= row.value <= Rewards.AGONY_TOP and row.curse_ceiling == Rewards.CURSE_CEILING, name
    assert await get_roll(S, 'nightbot') is None
    assert outcome.loser is not None and outcome.loser[1] <= Rewards.AGONY_TOP



async def test_ties_after_a_mass_flask_follow_its_random_walk(db, agony, monkeypatch):
    """Everyone gets the same number: the latest in the walk takes the loser's title, not the one
    who joined the game last – the rows are dated a millisecond apart in walk order."""
    await _players(first=50, second=60, buyer=70)
    monkeypatch.setattr(game.random, 'sample', lambda names, k: ['buyer', 'second', 'lurker', 'first'][:k])
    monkeypatch.setattr(game.random, 'randint', lambda a, b: 5)
    outcome = await redeem(game.Action.FLASK, 'buyer', chatters={'lurker'})
    assert outcome.loser == ('first', 5) and outcome.champion == ('lurker', 5)

async def test_without_the_chat_list_agony_takes_who_wrote_in_the_stream(db, agony):
    await _players(buyer=50)
    await save_tier(S, 'talker', Tier.REGULAR)
    outcome = await redeem(game.Action.FLASK, 'buyer', chatters=None)
    assert outcome.crowd == 2
    assert (await get_roll(S, 'talker')).value <= Rewards.AGONY_TOP


async def test_nothing_protects_from_agony_and_nothing_lifts_its_throws(db, agony, monkeypatch, top):
    await _players(buyer=50, shielded=90, lucky=80, warded=70)
    assert (await redeem(game.Action.SHIELD, 'shielded')).ok
    always(monkeypatch, game.Flask.LUCK)
    assert (await redeem(game.Action.FLASK, 'lucky')).ok
    await set_curse(S, 'warded', 55, None)
    assert (await redeem(game.Action.CLEANSE, 'buyer', 'warded')).ok
    always(monkeypatch, game.Flask.AGONY)

    assert (await redeem(game.Action.FLASK, 'buyer')).cursed == 4
    for name in ('shielded', 'lucky', 'warded'):
        assert (await get_roll(S, name)).value == Rewards.AGONY_TOP, name
    # The luck still holds, but while the agony holds every throw is capped at its top
    throw = await game.free_throw(S, 'lucky', limit=3)
    assert (throw.value, throw.agony, throw.luck) == (Rewards.AGONY_THROW_TOP, Rewards.AGONY_THROW_TOP, None)
    assert throw.next_ceiling == Rewards.CURSE_CEILING - Rewards.CURSE_STEP


async def test_a_curse_agony_laid_beats_the_luck_once_the_agony_is_over(db, agony, monkeypatch, top, clock):
    await _players(buyer=50, lucky=80)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    clock(Rewards.FLASK_MINUTES * 60)
    always(monkeypatch, game.Flask.LUCK)
    assert (await redeem(game.Action.FLASK, 'lucky')).ceiling == Rewards.CURSE_CEILING
    throw = await game.free_throw(S, 'lucky', limit=3)
    assert (throw.value, throw.agony, throw.luck) == (Rewards.CURSE_CEILING, None, None)


async def test_agony_doubles_a_running_curse_half_the_step_twice_the_hold(db, agony, monkeypatch, clock):
    await _players(buyer=50, victim=90, sglazed=60)
    await set_curse(S, 'victim', Rewards.CURSE_FLOOR, time.time())          # already on the floor
    await set_curse(S, 'sglazed', Rewards.CURSE_CEILING, None, time.time() + 600)
    monkeypatch.setattr(Rewards, 'AGONY_CURSE', 0.0)
    assert (await redeem(game.Action.FLASK, 'buyer')).cursed == 2
    sglazed = await get_roll(S, 'sglazed')
    assert sglazed.curse_until == pytest.approx(time.time() + 1200)
    thrown = await game.free_throw(S, 'sglazed', limit=3)
    assert thrown.next_ceiling == Rewards.CURSE_CEILING - Rewards.CURSE_STEP // 2
    # The floor's hold is doubled: past one hold the victim is still cursed, past two it lifts
    clock(Rewards.CURSE_HOLD_MINUTES * 60 + 1)
    assert rules.curse_of(await get_roll(S, 'victim'), time.time()) is not None
    # The Сглаз's doubled deadline, 20 minutes, has passed by now
    assert await game.lift_expired_curses(S) == ['sglazed']
    clock(2 * Rewards.CURSE_HOLD_MINUTES * 60)
    assert await game.lift_expired_curses(S) == ['victim']


async def test_while_agony_holds_only_throws_and_flasks_work(db, agony, clock, top):
    await _players(buyer=50, other=60)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    for action, user_input in ((game.Action.REROLL, 'other'), (game.Action.SHIELD, ''),
                               (game.Action.CLEANSE, 'other'), (game.Action.CURSE, 'other')):
        refused = await redeem(action, 'other', user_input)
        assert refused.status == game.Status.AGONY_LOCK, action
        assert refused.wait_minutes == Rewards.FLASK_MINUTES
    # The extra roll is a throw: it is judged on its own rules, not refused by the agony
    assert (await redeem(game.Action.EXTRA, 'other')).status == game.Status.FREE_LEFT
    assert (await redeem(game.Action.FLASK, 'other')).ok
    assert (await game.free_throw(S, 'other', limit=3)).value <= Rewards.AGONY_THROW_TOP
    assert (await game.status(S, 'other', limit=3)).agony_left == Rewards.FLASK_MINUTES
    clock(Rewards.FLASK_MINUTES * 60)
    assert (await redeem(game.Action.SHIELD, 'other')).ok


async def test_a_blessing_drunk_under_agony_ends_it(db, agony, monkeypatch, top):
    await _players(buyer=50, other=60)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    always(monkeypatch, game.Flask.BLESSING)
    assert (await redeem(game.Action.FLASK, 'other')).ok
    standing = await game.status(S, 'other', limit=3)
    assert (standing.agony_left, standing.blessed_left, standing.ceiling) == (None, Rewards.FLASK_MINUTES, None)
    assert (await game.free_throw(S, 'other', limit=3)).value == Roll.MAX
    assert (await redeem(game.Action.SHIELD, 'other')).status == game.Status.BLESS_LOCK

async def test_agonys_curses_are_not_the_buyers_purchases(db, agony):
    await _players(buyer=50, a=60, b=70)
    assert (await redeem(game.Action.FLASK, 'buyer')).cursed == 3
    assert await count_actions(S, 'buyer', game.Action.CURSE) == 0
    assert await count_actions(S, 'buyer', game.Action.FLASK) == 1


async def test_blessing_lifts_everyone_and_always_cleanses_the_cursed(db, blessing, monkeypatch):
    await _players(buyer=10, cursed=20, plain=30)
    await set_curse(S, 'cursed', 55, None)
    await add_perk(S, 'sglazed', game.Perk.CURSE, 'previous')
    monkeypatch.setattr(game.random, 'random', lambda: 0.0)   # the coin always says shield
    outcome = await redeem(game.Action.FLASK, 'buyer', chatters={'sglazed'})
    assert outcome.ok and outcome.flask == game.Flask.BLESSING
    assert (outcome.crowd, outcome.shields, outcome.cleanses) == (4, 2, 2)
    for name in ('buyer', 'cursed', 'plain', 'sglazed'):
        row = await get_roll(S, name)
        assert Rewards.BLESS_FLOOR <= row.value <= Roll.MAX and row.curse_ceiling is None, name
    assert (await get_perk(S, 'sglazed', game.Perk.CURSE)).consumed
    assert (await game.status(S, 'plain', limit=3)).shield_left is not None
    # None of it counts against the buyer's own purchases
    assert await count_actions(S, 'buyer', game.Action.SHIELD) == 0
    assert await count_actions(S, 'buyer', game.Action.CLEANSE) == 0


async def test_while_the_blessing_holds_only_throws_and_flasks_work(db, blessing, monkeypatch, clock):
    await _players(buyer=10, blessed=20)
    # A cleanse each, no shields: a swap may then draw the blessed
    monkeypatch.setattr(game.random, 'random', lambda: 0.99)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    await _players(late=30)
    for action in (game.Action.REROLL, game.Action.CURSE, game.Action.SHIELD, game.Action.CLEANSE):
        refused = await redeem(action, 'late', 'blessed')
        assert refused.status == game.Status.BLESS_LOCK, action
        assert refused.wait_minutes == Rewards.FLASK_MINUTES
    assert (await game.free_throw(S, 'blessed', limit=3)).ok
    assert (await game.status(S, 'late', limit=3)).blessed_left == Rewards.FLASK_MINUTES
    always(monkeypatch, game.Flask.SWAP)
    assert (await redeem(game.Action.FLASK, 'late')).ok
    clock(Rewards.FLASK_MINUTES * 60)
    assert (await redeem(game.Action.SHIELD, 'buyer')).ok

async def test_the_blessing_ends_a_choke_and_a_swaps_hold(db, blessing, monkeypatch):
    # The blessing's buyer has the swapper's own number: the swap cannot draw them
    await _players(choked=50, swapper=40, mate=60, buyer=40)
    always(monkeypatch, game.Flask.CHOKE)
    assert (await redeem(game.Action.FLASK, 'choked')).ok
    always(monkeypatch, game.Flask.SWAP)
    assert (await redeem(game.Action.FLASK, 'swapper')).ok
    always(monkeypatch, game.Flask.BLESSING)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    assert (await game.status(S, 'choked', limit=3)).choke_left is None
    assert (await game.status(S, 'swapper', limit=3)).flask_left is None
    assert (await game.free_throw(S, 'swapper', limit=3)).ok
    # A flask drunk under the blessing works as usual
    always(monkeypatch, game.Flask.CHOKE)
    assert (await redeem(game.Action.FLASK, 'mate')).ok
    assert (await game.status(S, 'mate', limit=3)).choke_left == Rewards.FLASK_MINUTES


async def test_agony_takes_back_what_the_blessing_gave(db, blessing, monkeypatch):
    await _players(buyer=10, blessed=20)
    monkeypatch.setattr(game.random, 'random', lambda: 0.0)   # shields for all, and every curse lands
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    assert (await game.status(S, 'blessed', limit=3)).shield_left is not None
    always(monkeypatch, game.Flask.AGONY)
    assert (await redeem(game.Action.FLASK, 'buyer')).cursed == 2
    blessed = await game.status(S, 'blessed', limit=3)
    assert (blessed.shield_left, blessed.blessed_left) == (None, None)
    assert blessed.value <= Rewards.AGONY_TOP and blessed.ceiling == Rewards.CURSE_CEILING


@pytest.mark.parametrize('flask', [game.Flask.SWAP, game.Flask.CHOKE])
async def test_a_cleanse_does_not_undo_a_flask_drunk_after_the_curse(db, monkeypatch, flask):
    """The roll before the curse comes back only if no swap or choke changed the number since."""
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: a)
    await _players(victim=90, mate=50)
    assert (await redeem(game.Action.CURSE, 'mate', 'victim')).value == Roll.MIN
    always(monkeypatch, flask)
    assert (await redeem(game.Action.FLASK, 'victim')).ok
    cleansed = await redeem(game.Action.CLEANSE, 'victim', 'victim')
    assert cleansed.ok and cleansed.value is None
    assert (await get_roll(S, 'victim')).value != 90


async def test_a_cleanse_still_returns_the_roll_after_an_old_flask(db, monkeypatch, clock):
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: a)
    monkeypatch.setattr(Rewards, 'CHOKE_STEP', 1)
    await _players(victim=90, mate=50)
    always(monkeypatch, game.Flask.CHOKE)
    assert (await redeem(game.Action.FLASK, 'victim')).ok
    clock(Rewards.FLASK_MINUTES * 60 + 1)
    await game.tick_chokes(S)
    before = (await get_roll(S, 'victim')).value
    assert (await redeem(game.Action.CURSE, 'mate', 'victim')).ok
    assert (await redeem(game.Action.CLEANSE, 'victim', 'victim')).value == before

@pytest.mark.parametrize(('agony', 'ceiling', 'expected'), [
    (None, None, (None, 100)), (None, 65, (65, 100)), (25, None, (None, 25)), (25, 65, (25, 25)),
])
def test_a_throw_under_agony_is_out_of_its_cap(agony, ceiling, expected):
    """«выбил 12 из 25»: under АГОНИЯ the chat names its cap, not the roll's top or a higher ceiling."""
    from src.twitch.local.roll.texts import out_of
    outcome = game.Outcome(game.Status.OK, value=12, ceiling=ceiling, agony=agony)
    assert out_of(outcome) == (expected[0], min(expected[1], Roll.MAX))


@pytest.mark.parametrize(('mass', 'low', 'high', 'chance'), [
    (game.Flask.AGONY, 25, 50, 1), (game.Flask.BLESSING, 75, 100, 4),
])
async def test_the_luck_is_half_as_strong_under_agony_and_twice_under_blessing(
    db, monkeypatch, mass, low, high, chance,
):
    await _players(buyer=50, lucky=60)
    monkeypatch.setattr(Rewards, 'AGONY_CURSE', 0.0)
    always(monkeypatch, game.Flask.LUCK)
    assert (await redeem(game.Action.FLASK, 'lucky')).ok
    always(monkeypatch, mass)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    monkeypatch.setattr(rules.random, 'random', lambda: 0.99)      # never the top
    seen = []
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: seen.append((a, b)) or a)
    throw = await game.free_throw(S, 'lucky', limit=3)
    assert seen == [(low, high - 1)] and throw.luck == chance
    assert throw.agony == (high if mass == game.Flask.AGONY else None)


@pytest.mark.parametrize(('mass', 'step'), [(None, 10), (game.Flask.AGONY, 20), (game.Flask.BLESSING, 5)])
async def test_the_choke_is_twice_as_strong_under_agony_and_half_under_blessing(db, monkeypatch, clock, mass, step):
    monkeypatch.setattr(Rewards, 'AGONY_CURSE', 0.0)
    await _players(choked=90, buyer=50)
    always(monkeypatch, game.Flask.CHOKE)
    assert (await redeem(game.Action.FLASK, 'choked')).ok
    if mass is not None:
        always(monkeypatch, mass)
        assert (await redeem(game.Action.FLASK, 'buyer')).ok
        # The blessing ends a running choke: a new one drunk under it is the one that steps
        if mass == game.Flask.BLESSING:
            always(monkeypatch, game.Flask.CHOKE)
            assert (await redeem(game.Action.FLASK, 'buyer')).ok
    who = 'buyer' if mass == game.Flask.BLESSING else 'choked'
    before = (await get_roll(S, who)).value
    clock(60)
    await game.tick_chokes(S)
    assert (await get_roll(S, who)).value == max(Roll.MIN, before - step)


async def test_a_lone_buyer_gets_no_flask_and_learns_nothing(db, monkeypatch):
    """With nobody a swap could draw the flask is refused before the draw, as without a roll:
    a refund after it would be a free redraw, and the text names no flask."""
    monkeypatch.setattr(game, 'FLASKS', {game.Flask.LUCK: 1})
    await _players(buyer=50, same=50)
    refused = await redeem(game.Action.FLASK, 'buyer')
    assert (refused.status, refused.flask) == (game.Status.NO_PARTNER, None)
    await _players(other=70)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok


async def test_a_cursed_lucky_player_is_already_cursed_not_guarded(db, luck):
    await _players(buyer=50)
    await set_curse(S, 'buyer', 55, None)
    assert (await redeem(game.Action.FLASK, 'buyer')).ok
    assert (await redeem(game.Action.CURSE, 'foe', 'buyer')).status == game.Status.ALREADY_CURSED
    standing = await game.status(S, 'buyer', limit=3)
    # The luck waits for the curse: its minutes run, no chance is promised
    assert (standing.luck_left, standing.luck_next) == (Rewards.FLASK_MINUTES, None)

# --- message pieces ------------------------------------------------------------

@pytest.mark.parametrize(('parts', 'text'), [
    (('@a выбил 95 из 100.', 'Китежанин: @a (95 из 100).', '(осталось 2)'),
     '@a выбил 95 из 100. Китежанин: @a (95 из 100) (осталось 2).'),
    (('@a выбил 3 из 100 – залупа!', '(осталось 2)', '(потолок 65)'),
     '@a выбил 3 из 100 – залупа (осталось 2) (потолок 65)!'),
    (('@a твой Roll 50 из 100.', '', 'Щит ещё 5 мин.'), '@a твой Roll 50 из 100. Щит ещё 5 мин.'),
    (('', '(осталось 2)'), '(осталось 2)'),
    (('Щит ещё 5 мин.', '(осталось 2)'), 'Щит ещё 5 мин. (осталось 2).'),
    (('Ура?!', '(осталось 2)'), 'Ура (осталось 2)?!'),
    (('Итак...', '(осталось 2)', '(потолок 65)'), 'Итак (осталось 2) (потолок 65)...'),
    (('Без точки', '(осталось 2)'), 'Без точки (осталось 2)'),
])
def test_a_note_in_brackets_goes_before_the_closing_mark(parts, text):
    assert compose(*parts) == text


async def _rollstat(user: str, args: str = '', badges: dict | None = None) -> list[str]:
    text = f'!rollstat {args}'.strip()
    message = make_message(text, make_chatter(user, **(badges or {})))
    await handle_rollstat(CommandContext(message=message, user=user, prompt=text, original_text=text,
                                         session_id=S, bot=FakeBot(session_id=S), args=args))
    return [call.args[0] for call in message.respond.await_args_list]


async def test_rollstat_with_a_nick_tells_about_that_player(db):
    await save_roll(S, 'other', 40, free_throw=True)
    await save_tier(S, 'other', Tier.VIP)
    [answer] = await _rollstat('asker', '@Other')
    assert answer.startswith('texts.rollstat_self_other')
    # Their own limit by the status chat last wrote for them, not the asker's
    assert (await game.status(S, 'other', limit=rules.free_limit(Tier.VIP))).free_left == Roll.FREE_VIP - 1
    assert 'texts.roll_free_left' in answer


async def test_rollstat_on_a_cursed_player_speaks_of_them(db):
    await save_roll(S, 'other', 40, free_throw=False)
    await set_curse(S, 'other', 55, None)
    [answer] = await _rollstat('asker', 'other')
    assert answer.startswith('texts.rollstat_cursed_other')


async def test_rollstat_on_a_player_who_has_not_thrown(db):
    assert (await _rollstat('asker', 'nobody'))[0].startswith('texts.rollstat_none_other')


async def test_rollstat_with_your_own_nick_is_your_own(db):
    await save_roll(S, 'asker', 40, free_throw=True)
    assert (await _rollstat('asker', 'Asker'))[0].startswith('texts.rollstat_self ')


async def test_rollstat_with_more_than_a_nick_is_refused(db):
    assert await _rollstat('asker', 'два слова') == ['texts.rollstat_bad_input']
