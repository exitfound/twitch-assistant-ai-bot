"""The «залупа стрима» (session loser) game: the only place that changes the rolls table.

A throw from chat and channel-points rewards come from different places, but
they share one state. Everything that reads a roll and then writes a new one runs
under a shared lock: otherwise a reroll arriving between the read and the write
of the victim's own !roll would overwrite one of the results.

The module writes nothing to chat and knows nothing about Twitch. It returns an
Outcome, and the caller – the command handler or the reward handler – decides the
text and the fate of the points from it.
"""
import asyncio
import dataclasses
import random
import time
from collections.abc import Collection
from enum import StrEnum
from typing import NamedTuple

from src.core.config import Rewards, Roll, Twitch
from src.core.database import get_previous_stream_session, get_session_start, has_chatted, transaction
from src.twitch.core.viewer import Tier, by_tier
from src.twitch.local.roll import rules
from src.twitch.local.roll.storage import (
    RollRow, activate_perks, add_choke, add_perk, clear_chokes, clear_chokes_outside, clear_curses_outside,
    consume_perk, consume_perks, count_actions, count_throws_since, flask_drinks, flask_granted_at, flask_last_at,
    get_action_status, get_chokes, get_due_chokes, get_expired_curses, get_held, get_last_roll_session_before,
    get_perk, get_players, get_present, get_roll, get_rolls, get_session_titles, get_shielded, get_tier,
    get_waiting_curses, last_move_at, last_throws, move_choke, reroll_times, restore_roll, revoke_grants,
    save_action, save_grants, save_roll, save_rolls, save_throw, seconds_since_action, set_curse, set_curses,
    set_roll_value, swap_holds, value_before_curse,
)

class Action(StrEnum):
    """Actions bought with channel points."""
    EXTRA = 'extra'      # a throw for yourself beyond the free ones
    REROLL = 'reroll'    # a throw for someone else, the shield protects from it
    CURSE = 'curse'      # a capped throw for another, then the cap drops; pierces the shield
    SHIELD = 'shield'    # protection from rerolls for SHIELD_MINUTES
    CLEANSE = 'cleanse'  # lifts a curse, any player's, returns the roll it threw over, keeps new ones off
    FLASK = 'flask'      # a random flask from FLASKS


class Flask(StrEnum):
    """What a random flask can come up with. Each lasts FLASK_MINUTES."""
    SWAP = 'swap'        # the buyer's roll swaps with a random player's, both are out of rerolls
    LUCK = 'luck'        # the buyer's own throws go higher and hit the top more often, no curse lands
    CHOKE = 'choke'      # the buyer's roll loses a step every minute, whatever they throw
    AGONY = 'agony'      # everyone in chat: a low roll and a coin-flip curse; then low throws, no spells
    BLESSING = 'blessing'  # everyone in chat: a high roll and a shield or a cleanse; then no spells


# The flasks the reward draws from and their weights, in percent
FLASKS: dict[Flask, int] = {Flask.SWAP: 30, Flask.LUCK: 30, Flask.CHOKE: 30, Flask.AGONY: 5, Flask.BLESSING: 5}

# The flasks that reach everyone in chat: only they need Twitch's chat list
MASS = (Flask.AGONY, Flask.BLESSING)


def draw_flask() -> Flask:
    """A flask by FLASKS' weights. Drawn before the game's lock, so a mass flask's chat list is
    fetched only when one came up; a refusal before the draw's use keeps it hidden."""
    return random.choices(tuple(FLASKS), weights=tuple(FLASKS.values()))[0]

# The choking flask's step comes every minute, counted from the drink
CHOKE_SECONDS = 60


class Perk(StrEnum):
    """Perks from the previous stream: not bought, granted at the start of the new one."""
    SHIELD = 'shield'    # to the китежанин (champion) – a shield from rerolls
    CURSE = 'curse'      # to the залупа (loser) – a curse with a hard deadline


class Status(StrEnum):
    """Outcomes. OK – the change is applied, everything else is a refusal with a reason."""
    OK = 'ok'
    NO_FREE_LEFT = 'no_free_left'           # !roll: free throws are used up
    FREE_LEFT = 'free_left'                 # extra roll bought while free throws are still left
    TOO_FAST = 'too_fast'                   # a throw of your own right after a burst of them
    REROLL_LIMIT = 'reroll_limit'           # the buyer's rerolls for this window are used up
    SHIELD_LIMIT = 'shield_limit'           # the buyer's shields for this stream are used up
    CURSE_LIMIT = 'curse_limit'             # the buyer's curses for this stream are used up
    CLEANSE_LIMIT = 'cleanse_limit'         # the buyer's cleanses for this stream are used up
    FLASK_LIMIT = 'flask_limit'             # the buyer's flasks for this stream are used up
    UNDER_FLASK = 'under_flask'             # a throw or reroll by a player a swap holds
    FLASK_GUARDED = 'flask_guarded'         # a reroll on a player a swap holds
    NO_ROLL = 'no_roll'                     # a flask by a buyer who has not rolled
    NO_PARTNER = 'no_partner'               # a flask while a swap would have nobody to swap with
    LUCK_GUARDED = 'luck_guarded'           # a curse on a player the luck flask holds
    AGONY_LOCK = 'agony_lock'               # a reroll, curse, shield or cleanse while АГОНИЯ ЧЕРНОМИРЬЯ holds
    BLESS_LOCK = 'bless_lock'               # the same while БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА holds
    REVOKED = 'revoked'                     # journal only: a mass flask's grant the next one took back
    BAD_TARGET = 'bad_target'               # the reward input is not a nick
    EXTRA_WORDS = 'extra_words'             # the reward input holds more than one word
    SELF_TARGET = 'self_target'             # rerolling or cursing yourself
    NOT_ROLLED = 'not_rolled'               # the target has not rolled today
    SHIELDED = 'shielded'                   # the target has a shield
    ALREADY_SHIELDED = 'already_shielded'   # shield bought a second time
    ALREADY_CURSED = 'already_cursed'       # the target is already cursed
    PROTECTED = 'protected'                 # the target was rerolled recently, protection still holds
    UNKNOWN_TARGET = 'unknown_target'       # reroll for a nick seen neither in the game nor in chat
    PERK_SHIELDED = 'perk_shielded'         # the target has the previous stream's champion shield
    NOT_CURSED = 'not_cursed'               # cleanse of a player with no curse to lift
    CLEANSED = 'cleansed'                   # curse on a player cleansed recently, protection still holds
    DUPLICATE = 'duplicate'                 # Twitch sent the same redemption again
    OFFLINE = 'offline'                     # bought as the stream ended, the rewards paused
    ERROR = 'error'                         # the game failed to apply it: journaled against redelivery
    NOT_FOLLOWER = 'not_follower'           # the buyer does not follow, the game is closed to them


_lock = asyncio.Lock()


@dataclasses.dataclass(frozen=True)
class Outcome:
    status: Status
    target: str | None = None               # whose roll is affected
    old_value: int | None = None            # roll before the operation
    value: int | None = None                # roll after the operation
    free_left: int | None = None            # free throws left, None – no limit
    loser: tuple[str, int] | None = None    # session loser after the operation
    champion: tuple[str, int] | None = None # session champion; a lone player is it, with no loser
    # The target's curse, if it applied to this throw
    ceiling: int | None = None              # ceiling of this throw
    next_ceiling: int | None = None         # ceiling of the next one
    curse_minutes_left: int | None = None   # ceiling on the floor: minutes until it lifts
    # A protection's minutes: left on a shield, a Благодать, the respite after a
    # reroll or the ward after a cleanse; the full ward on a cleanse that succeeded
    guard_minutes: int | None = None
    # Minutes until the buyer or player may act again: a spam pause or a new reroll window
    wait_minutes: int | None = None
    pause_seconds: float | None = None      # too fast: seconds until the next own throw
    limit: int | None = None                # rerolls per window, other rewards per stream
    sglaz: bool = False                     # the previous stream's Сглаз was laid on this throw
    flask: str | None = None                # which flask the random flask came up with
    luck: int | None = None                 # the luck flask: the chance of the top, in percent, this throw had
    agony: int | None = None                # АГОНИЯ ЧЕРНОМИРЬЯ capped this throw at that number
    mode: str | None = None                 # a flask's outcome: the mass flask holding the game as it was drunk
    stacks: int | None = None               # the luck flask: how many of them hold the buyer now, this one too
    extra: int | None = None                # a swap: minutes the buyer is held beyond FLASK_MINUTES
    # A mass flask: how many it reached, how many АГОНИЯ cursed, how many БЛАГОСЛОВЕНИЕ shielded
    # and cleansed
    crowd: int | None = None
    cursed: int | None = None
    shields: int | None = None
    cleanses: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == Status.OK


@dataclasses.dataclass(frozen=True)
class Throw:
    """A throw on a player's row and what the curse did to it, if there was one."""
    value: int
    ceiling: int | None = None
    next_ceiling: int | None = None
    curse_minutes_left: int | None = None
    sglaz: bool = False
    luck: int | None = None
    agony: int | None = None    # АГОНИЯ ЧЕРНОМИРЬЯ capped this throw at that number


@dataclasses.dataclass(frozen=True)
class Standings:
    """The session's two titles: the loser (залупа) and the champion (китежанин)."""
    loser: tuple[str, int] | None
    champion: tuple[str, int] | None


def _thrown(target: str, old_value: int | None, throw: Throw, standings: Standings, **extra) -> Outcome:
    """A successful throw as an outcome."""
    return Outcome(
        Status.OK, target=target, old_value=old_value, value=throw.value, ceiling=throw.ceiling,
        next_ceiling=throw.next_ceiling, curse_minutes_left=throw.curse_minutes_left,
        sglaz=throw.sglaz, luck=throw.luck, agony=throw.agony, loser=standings.loser, champion=standings.champion,
        **extra,
    )


async def _throw_for(
    session_id: str, user: str, row: RollRow | None, *, free_throw: bool, own: bool = False,
) -> Throw:
    """A throw on a player's row – their own (own=True) or someone else's reroll.

    A cursed throw is capped by the ceiling, which drops a step on any throw on the victim. Own
    throws under the luck flask are lucky unless a curse holds – it beats the luck. Under АГОНИЯ
    every throw is capped (a lucky one at its weaker top) and the previous stream's Сглаз waits.
    """
    curse = rules.curse_of(row, time.time())
    power = row.curse_power if curse is not None else None
    mode = await _mass(session_id)
    agony = rules.agony_throw_top() if mode == Flask.AGONY else None
    run = await _luck_run(session_id, user) if curse is None else None
    if run is not None:
        if not own:
            value = rules.throw(agony)
            await save_roll(session_id, user, value, free_throw=free_throw)
            return Throw(value, agony=agony)
        luck = rules.luck_of(mode, run.stacks)
        throw_no = await count_throws_since(session_id, user, run.start) + 1
        value = rules.lucky_throw(throw_no, luck)
        await save_roll(session_id, user, value, free_throw=free_throw)
        return Throw(value, luck=round(100 * rules.luck_chance(throw_no, luck)),
                     agony=luck.top if agony is not None else None)
    until = row.curse_until if curse is not None else None
    sglaz = False
    if curse is None and agony is None:
        # The previous stream's loser curse is laid on the first throw on them
        until = await _take_perk_curse(session_id, user)
        if until is not None:
            curse, sglaz = (Rewards.CURSE_CEILING, None), True
    if curse is None:
        value = rules.throw(agony)
        await save_roll(session_id, user, value, free_throw=free_throw)
        return Throw(value, agony=agony)
    ceiling, floor_at = curse
    value = rules.throw(ceiling if agony is None else min(ceiling, agony))
    await save_roll(session_id, user, value, free_throw=free_throw)
    now = time.time()
    next_ceiling, floor_at = rules.lowered(ceiling, floor_at, now, power)
    await set_curse(session_id, user, next_ceiling, floor_at, until, power)
    return Throw(value, ceiling, next_ceiling, rules.minutes_left(floor_at, until, now, power), sglaz,
                 agony=agony)


async def _standings(session_id: str) -> Standings:
    """The session's two titles by storage.get_session_titles(), the rule memory reads too."""
    champion, loser = await get_session_titles(session_id)
    return Standings(loser, champion)


async def titles(session_id: str) -> Standings:
    """The session's two titles, read only: what the overlay shows on connect."""
    return await _standings(session_id)


async def last_move(session_id: str) -> float | None:
    """When the session's last throw or redemption was, by the bot's clock. Read only."""
    return await last_move_at(session_id)


# --- perks from the previous stream ------------------------------------------------

async def _activate_perks(session_id: str, user: str) -> list[str]:
    """A player's first appearance in the stream starts the countdown of their perks."""
    now = time.time()
    return await activate_perks(session_id, user, now, now + Roll.PERK_MINUTES * 60)


async def _perk_shield_left(session_id: str, user: str) -> int | None:
    """Minutes until the champion's shield ends. None – no shield.

    A reroll on a player is also their appearance: the shield turns on and protects at once.
    """
    await _activate_perks(session_id, user)
    perk = await get_perk(session_id, user, Perk.SHIELD)
    if perk is None or perk.active_until is None:
        return None
    return rules.whole_minutes(perk.active_until - time.time())


async def _take_perk_curse(session_id: str, user: str) -> float | None:
    """Take the loser's curse for the first throw on them. Returns its deadline.

    There is one curse: once laid on a throw it is not given a second time, even
    if it was lifted early. A player who did not show up in time loses it.
    """
    await _activate_perks(session_id, user)
    perk = await get_perk(session_id, user, Perk.CURSE)
    if perk is None or perk.consumed or perk.active_until is None or perk.active_until <= time.time():
        return None
    await consume_perk(session_id, user, Perk.CURSE)
    return perk.active_until


async def _previous_session(session_id: str) -> str | None:
    """The session whose results grant the perks: the stream before this one.

    The previous stream had no rolls – no perks, earlier streams do not count.
    No earlier streams recorded (the first stream after the switch from date
    sessions) – the latest old session whose throws ended before this stream started.
    """
    previous = await get_previous_stream_session(session_id)
    if previous is not None:
        return previous
    start = await get_session_start(session_id)
    return await get_last_roll_session_before(session_id, start if start is not None else time.time())


async def grant_perks(session_id: str, *, shield: bool = True) -> tuple[str | None, str | None] | None:
    """Grant the perks from the previous stream: (champion, loser).

    None – nothing to grant or everything is already granted. Safe to repeat after
    a restart or a stream outage: nothing is granted a second time. shield=False holds
    the champion's shield back: it guards only against rerolls, which need the rewards.
    """
    async with _lock, transaction():
        previous = await _previous_session(session_id)
        if previous is None:
            return None
        standings = await _standings(previous)
        champion, loser = standings.champion, standings.loser
        shielded = (shield and champion is not None
                    and await add_perk(session_id, champion[0], Perk.SHIELD, previous))
        cursed = loser is not None and await add_perk(session_id, loser[0], Perk.CURSE, previous)
    if not (shielded or cursed):
        return None
    return (champion[0] if shielded else None, loser[0] if cursed else None)


async def appear(session_id: str, user: str) -> list[str]:
    """A player showed up in the stream chat: start their perk countdown. Returns which started."""
    async with _lock, transaction():
        return await _activate_perks(session_id, user)


# --- operations --------------------------------------------------------------

async def free_throw(session_id: str, user: str, *, limit: int, unlimited: bool = False) -> Outcome:
    """A free !roll: no more than limit per session, by the viewer's status (rules.free_limit()).

    unlimited (broadcaster) still counts the throw, so the paid extra roll behaves the
    same for them, and free_left is None since there is no remainder to mention.
    """
    async with _lock, transaction():
        row = await get_roll(session_id, user)
        used = row.free_throws if row else 0
        if not unlimited and used >= limit:
            return Outcome(Status.NO_FREE_LEFT, target=user, free_left=0)
        if held := await _flask_left(session_id, user):
            return Outcome(Status.UNDER_FLASK, target=user, wait_minutes=held)
        if not unlimited and (wait := await _burst_left(session_id, user)):
            return _too_fast(user, wait)
        throw = await _throw_for(session_id, user, row, free_throw=True, own=True)
        await save_throw(session_id, user, time.time())
        standings = await _standings(session_id)
    return _thrown(user, row.value if row else None, throw, standings,
                   free_left=None if unlimited else max(0, limit - used - 1))


@dataclasses.dataclass(frozen=True)
class Standing:
    """What !rollstat shows: the player's own state plus the session's two titles."""
    value: int | None = None                # their roll, None – they have not rolled
    free_left: int | None = None            # free throws left, None – no limit
    ceiling: int | None = None              # curse: ceiling of their next throw
    curse_minutes_left: int | None = None   # ceiling on the floor: minutes until it lifts
    curse_power: int | None = None          # how many times АГОНИЯ doubled the curse, None – not doubled
    shield_left: int | None = None          # a shield bought with points: minutes left
    shield_minutes_left: int | None = None  # the previous stream's champion shield
    flask_left: int | None = None           # a swap holds their roll: minutes left
    luck_left: int | None = None            # the luck flask holds: minutes left
    luck_next: int | None = None            # and the chance of the top their next throw has, percent
    luck_stacks: int | None = None          # and how many luck flasks hold them
    choke_left: int | None = None           # the choking flask wears their roll down: minutes left
    choke_stacks: int | None = None         # and how many chokes run on them
    blessed_left: int | None = None         # БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА holds the game: minutes left
    agony_left: int | None = None           # АГОНИЯ ЧЕРНОМИРЬЯ holds the game: minutes left
    loser: tuple[str, int] | None = None
    champion: tuple[str, int] | None = None


async def status(session_id: str, user: str, *, limit: int, unlimited: bool = False) -> Standing:
    """The player's standing in the session. Reads only: no throw, no perk started.

    _perk_shield_left() is not used on purpose – it activates the perk, and merely
    asking about your state must not start the countdown.
    """
    row = await get_roll(session_id, user)
    now = time.time()
    curse = rules.curse_of(row, now)
    perk = await get_perk(session_id, user, Perk.SHIELD)
    left = None
    if perk is not None and not perk.consumed and perk.active_until is not None:
        left = rules.whole_minutes(perk.active_until - now)
    standings = await _standings(session_id)
    mass, mass_left = await _mass_state(session_id)
    run = await _luck_run(session_id, user)
    luck_next = None
    # Under a curse the luck waits: its minutes run, but no lucky throw comes, so no chance either
    if run is not None and curse is None:
        throw_no = await count_throws_since(session_id, user, run.start) + 1
        luck_next = round(100 * rules.luck_chance(throw_no, rules.luck_of(mass, run.stacks)))
    chokes = await get_chokes(session_id, user, now)
    return Standing(
        value=row.value if row else None,
        free_left=None if unlimited else max(0, limit - (row.free_throws if row else 0)),
        ceiling=curse[0] if curse else None,
        curse_minutes_left=rules.minutes_left(curse[1], row.curse_until, now, row.curse_power) if curse else None,
        curse_power=row.curse_power if curse and (row.curse_power or 1) > 1 else None,
        shield_left=await _shield_left(session_id, user),
        shield_minutes_left=left,
        flask_left=await _flask_left(session_id, user),
        luck_left=rules.whole_minutes(run.end - now) if run else None,
        luck_next=luck_next,
        luck_stacks=run.stacks if run else None,
        choke_left=rules.whole_minutes(chokes[-1] - now) if chokes else None,
        choke_stacks=len(chokes) or None,
        blessed_left=mass_left if mass == Flask.BLESSING else None,
        agony_left=mass_left if mass == Flask.AGONY else None,
        loser=standings.loser,
        champion=standings.champion,
    )


async def lift_expired_curses(session_id: str) -> list[str]:
    """Lift expired curses: the ceiling sat on the floor longer than CURSE_HOLD_MINUTES
    or the hard deadline of the previous stream's loser curse passed.

    The mechanics do not need the lift: rules.curse_of() already stops treating such a
    row as cursed. It is there so the bot says so in chat exactly once – a cleared
    row will not be selected again. Under the shared lock, so as not to erase a
    curse laid anew between the select and the write.
    """
    async with _lock, transaction():
        now = time.time()
        users = await get_expired_curses(session_id, Rewards.CURSE_HOLD_MINUTES * 60, now)
        for user in users:
            await set_curse(session_id, user, None, None)
    return users


async def drop_finished_curses(session_id: str) -> int:
    """Clear the curses left in sessions before this stream. Returns how many.

    A curse that never reached the floor, or a stream that ended within its hold, leaves
    the columns set: the lift checks only the current session. At a stream's start every
    other session is over – a resumed stream keeps its own session – so they go silently,
    with the chokes still running in them.
    """
    async with _lock, transaction():
        await clear_chokes_outside(session_id)
        return await clear_curses_outside(session_id)


async def redeem(
    action: str, session_id: str, actor: str, user_input: str, redemption_id: str,
    *, flask: Flask | None = None, chatters: Collection[str] | None = None,
) -> Outcome:
    """Apply a bought reward and record the outcome in the roll_actions journal.

    The journal also guards against duplicates: Twitch may send one event twice,
    and the second one must not throw. The check and the write run under the same
    lock as the throw itself, so a duplicate cannot slip in between them.
    flask – the flask drawn for a flask reward (draw_flask()), None – draw it here; chatters – who
    Twitch says is in chat, for a mass flask, None – Twitch did not say.
    """
    async with _lock, transaction():
        if await get_action_status(redemption_id) is not None:
            return Outcome(Status.DUPLICATE)
        mass, held = await _mass_state(session_id)
        if mass is not None and action in _MASS_LOCKED:
            status = Status.AGONY_LOCK if mass == Flask.AGONY else Status.BLESS_LOCK
            outcome = Outcome(status, target=actor, wait_minutes=held)
        elif action == Action.EXTRA:
            outcome = await _extra(session_id, actor)
        elif action == Action.SHIELD:
            outcome = await _shield(session_id, actor)
        elif action == Action.REROLL:
            outcome = await _reroll(session_id, actor, user_input)
        elif action == Action.CURSE:
            outcome = await _curse(session_id, actor, user_input)
        elif action == Action.CLEANSE:
            outcome = await _cleanse(session_id, actor, user_input)
        elif action == Action.FLASK:
            outcome = await _flask(session_id, actor, redemption_id, flask or draw_flask(), chatters)
        else:
            raise ValueError(f'Неизвестное действие: {action}')
        await save_action(
            redemption_id, session_id, action, actor, user_input,
            outcome.target, outcome.old_value, outcome.value, outcome.status, outcome.flask,
            acted_at=time.time(), actor_extra=outcome.extra * 60 if outcome.extra else None,
        )
    return outcome


# What a mass flask shuts for its minutes: throws and flasks still work
_MASS_LOCKED = (Action.REROLL, Action.CURSE, Action.SHIELD, Action.CLEANSE)


async def refuse(
    action: str, session_id: str, actor: str, user_input: str, redemption_id: str, status: Status,
) -> bool:
    """Record a redemption refused before the game saw it (offline, no follow).

    False – it is in the journal already: a redelivered refusal is neither said in
    chat again nor given a second status on Twitch.
    """
    async with _lock, transaction():
        if await get_action_status(redemption_id) is not None:
            return False
        await save_action(redemption_id, session_id, action, actor, user_input, None, None, None, status)
    return True


async def _extra(session_id: str, actor: str) -> Outcome:
    row = await get_roll(session_id, actor)
    used = row.free_throws if row else 0
    # A redemption event carries no badges: the status is the one chat last showed
    limit = rules.free_limit(Tier(await get_tier(session_id, actor) or Tier.REGULAR))
    if used < limit:
        # Points for a throw that is free anyway are almost certainly a misclick
        return Outcome(Status.FREE_LEFT, target=actor, free_left=limit - used)
    if held := await _flask_left(session_id, actor):
        return Outcome(Status.UNDER_FLASK, target=actor, wait_minutes=held)
    # A redemption carries no badges: the broadcaster is known by the channel's login
    if actor != (Twitch.CHANNEL or '').lower() and (wait := await _burst_left(session_id, actor)):
        return _too_fast(actor, wait)
    throw = await _throw_for(session_id, actor, row, free_throw=False, own=True)
    await save_throw(session_id, actor, time.time())
    return _thrown(actor, row.value if row else None, throw, await _standings(session_id))


async def _burst_left(session_id: str, user: str) -> float | None:
    """Seconds the player waits after throwing too fast. None – they may throw now.

    Free !roll and paid extra rolls count together: Twitch has no per-viewer cooldown
    on a reward, and subscribers have no cooldown on !roll.
    """
    times = await last_throws(session_id, user, Roll.BURST_THROWS)
    return rules.burst_pause_left(times, time.time(), Roll.BURST_THROWS, Roll.BURST_SECONDS,
                                  Roll.BURST_PAUSE_MINUTES * 60)


def _too_fast(user: str, wait: float) -> Outcome:
    return Outcome(Status.TOO_FAST, target=user, wait_minutes=rules.whole_minutes(wait),
                   pause_seconds=wait)


async def _shield(session_id: str, actor: str) -> Outcome:
    if limit := await _stream_limit_reached(session_id, actor, Action.SHIELD):
        return Outcome(Status.SHIELD_LIMIT, target=actor, limit=limit)
    # The champion's shield counts too: a second one on top of it would add nothing
    left = await _shield_left(session_id, actor) or await _perk_shield_left(session_id, actor)
    if left is not None:
        return Outcome(Status.ALREADY_SHIELDED, target=actor, guard_minutes=left)
    return Outcome(Status.OK, target=actor)


async def _shield_left(session_id: str, user: str) -> int | None:
    """Minutes the bought shield still holds. None – no shield.

    The shield is a successful journal row with no state of its own: it holds
    SHIELD_MINUTES from the latest purchase.
    """
    elapsed = await seconds_since_action(session_id, Action.SHIELD, user, Status.OK, time.time())
    if elapsed is None:
        return None
    return rules.whole_minutes(Rewards.SHIELD_MINUTES * 60 - elapsed)


async def _target(
    session_id: str, actor: str, user_input: str, *, require_roll: bool, allow_self: bool = False,
) -> tuple[str, RollRow | None] | Outcome:
    """The target of a reroll or curse – or a refusal if there is nobody to touch.

    require_roll – the target must have rolled this session: a curse is laid only
    on someone already in the game. A reroll also throws for someone who has not
    rolled yet, but only for a nick that has written in chat at least once:
    otherwise a typo in the reward input would create a roll for a nonexistent
    player, who could become the «залупа стрима».
    """
    if len(user_input.split()) > 1:
        # «nick1 nick2», «@nick спасибо»: which nick was meant is a guess, so none is taken
        return Outcome(Status.EXTRA_WORDS)
    target = rules.parse_nick(user_input)
    if target is None:
        return Outcome(Status.BAD_TARGET)
    if target == actor and not allow_self:
        return Outcome(Status.SELF_TARGET, target=target)
    row = await get_roll(session_id, target)
    if row is None:
        if require_roll:
            return Outcome(Status.NOT_ROLLED, target=target)
        if not await has_chatted(target):
            return Outcome(Status.UNKNOWN_TARGET, target=target)
    return target, row


async def _protection_left(session_id: str, target: str) -> int | None:
    """Minutes until the protection after someone's reroll ends. None – no protection.

    Twitch's per-viewer limit counts each attacker separately, so several people
    could reroll one target in a row. The window runs from the last successful
    reroll, and refused attempts are journaled as something other than ok and do
    not extend it.
    """
    window = Rewards.REROLL_PROTECT_MINUTES * 60
    if not window:
        return None
    elapsed = await seconds_since_action(session_id, Action.REROLL, target, Status.OK, time.time())
    if elapsed is None:
        return None
    return rules.whole_minutes(window - elapsed)


async def _reroll(session_id: str, actor: str, user_input: str) -> Outcome:
    if held := await _flask_left(session_id, actor):
        return Outcome(Status.UNDER_FLASK, target=actor, wait_minutes=held)
    limit = await _buyer_limit(session_id, actor)
    if limit is not None:
        wait = rules.window_left(await reroll_times(session_id, actor), time.time(), limit,
                                 Rewards.REROLL_WINDOW_MINUTES * 60)
        if wait is not None:
            return Outcome(Status.REROLL_LIMIT, wait_minutes=rules.whole_minutes(wait), limit=limit)
    found = await _target(session_id, actor, user_input, require_roll=False)
    if isinstance(found, Outcome):
        return found
    target, row = found
    if held := await _flask_left(session_id, target):
        return Outcome(Status.FLASK_GUARDED, target=target, guard_minutes=held)
    shield_left = await _shield_left(session_id, target)
    if shield_left is not None:
        return Outcome(Status.SHIELDED, target=target, guard_minutes=shield_left)
    perk_left = await _perk_shield_left(session_id, target)
    if perk_left is not None:
        return Outcome(Status.PERK_SHIELDED, target=target, guard_minutes=perk_left)
    protect_left = await _protection_left(session_id, target)
    if protect_left is not None:
        return Outcome(Status.PROTECTED, target=target, guard_minutes=protect_left)
    # On a cursed target the reroll is capped by their ceiling and lowers it the same
    # way the victim's own throw does. The victim's free throws are not spent
    throw = await _throw_for(session_id, target, row, free_throw=False)
    return _thrown(target, row.value if row else None, throw, await _standings(session_id))


async def _stream_limit_reached(session_id: str, actor: str, action: str) -> int | None:
    """The buyer's limit for this action per stream once it is used up, None – they may buy.

    Only successful purchases count: a refused one gave the points back.
    """
    limit = await _buyer_limit(session_id, actor)
    if limit is not None and await count_actions(session_id, actor, action) >= limit:
        return limit
    return None


async def _buyer_limit(session_id: str, actor: str) -> int | None:
    """How many rerolls per window, or shields, curses and cleanses per stream, the buyer
    may make, None – no limit (the broadcaster, or a REWARD_LIMIT_* of 0).

    A redemption carries no badges: the status is the one chat last showed (roll_tiers),
    and a buyer who has not written in this stream counts as a follower.
    """
    if actor == (Twitch.CHANNEL or '').lower():
        return None
    tier = await get_tier(session_id, actor) or Tier.REGULAR
    limit = by_tier(Tier(tier), sub=Rewards.LIMIT_SUB, mod=Rewards.LIMIT_MOD, vip=Rewards.LIMIT_VIP,
                    regular=Rewards.LIMIT_FOLLOWER, broadcaster=None)
    return limit or None


async def _curse(session_id: str, actor: str, user_input: str) -> Outcome:
    if limit := await _stream_limit_reached(session_id, actor, Action.CURSE):
        return Outcome(Status.CURSE_LIMIT, limit=limit)
    found = await _target(session_id, actor, user_input, require_roll=True)
    if isinstance(found, Outcome):
        return found
    target, row = found
    if rules.curse_of(row, time.time()) is not None:
        # A new curse would reset the ceiling to the top – a gift to the victim. Checked first:
        # a cursed player under the luck is cursed already, not guarded by the luck
        return Outcome(Status.ALREADY_CURSED, target=target)
    # A swap's hold keeps off rerolls only: the curse reaches a swapped player too
    if lucky := await _luck_left(session_id, target):
        return Outcome(Status.LUCK_GUARDED, target=target, guard_minutes=lucky)
    # The shield is deliberately not checked: the curse pierces it, that is what sets
    # it apart from the cheap reroll
    cleansed_left = await _cleanse_protection_left(session_id, target)
    if cleansed_left is not None:
        return Outcome(Status.CLEANSED, target=target, guard_minutes=cleansed_left)
    now = time.time()
    ceiling = Rewards.CURSE_CEILING
    value = rules.throw(ceiling)
    await save_roll(session_id, target, value, free_throw=False)
    next_ceiling, floor_at = rules.lowered(ceiling, None, now)
    await set_curse(session_id, target, next_ceiling, floor_at)
    throw = Throw(value, ceiling, next_ceiling, rules.minutes_left(floor_at, None, now))
    return _thrown(target, row.value, throw, await _standings(session_id))


async def _cleanse(session_id: str, actor: str, user_input: str) -> Outcome:
    """Lift the target's curse: the ceiling is gone at once, and the roll a bought curse
    threw over comes back if it is higher than the current one.

    The previous stream's loser curse not laid yet is cancelled too, or it would land
    on the next throw. The target may be the buyer.
    """
    if limit := await _stream_limit_reached(session_id, actor, Action.CLEANSE):
        return Outcome(Status.CLEANSE_LIMIT, limit=limit)
    found = await _target(session_id, actor, user_input, require_roll=False, allow_self=True)
    if isinstance(found, Outcome):
        return found
    target, row = found
    curse = rules.curse_of(row, time.time())
    pending = await _pending_perk_curse(session_id, target)
    if curse is None and not pending:
        return Outcome(Status.NOT_CURSED, target=target)
    if row is not None:
        await set_curse(session_id, target, None, None)
    if pending:
        await consume_perk(session_id, target, Perk.CURSE)
    ceiling = curse[0] if curse else None
    # A bought curse has no deadline and threw over the roll; the previous stream's
    # curse fell on a first throw, so there is nothing to return
    if curse is not None and row.curse_until is None:
        before = await value_before_curse(session_id, target, Rewards.FLASK_MINUTES * 60)
        if before is not None and before > row.value:
            await restore_roll(session_id, target, before)
            standings = await _standings(session_id)
            return Outcome(Status.OK, target=target, old_value=row.value, value=before, ceiling=ceiling,
                           guard_minutes=Rewards.CLEANSE_PROTECT_MINUTES,
                           loser=standings.loser, champion=standings.champion)
    return Outcome(Status.OK, target=target, ceiling=ceiling,
                   guard_minutes=Rewards.CLEANSE_PROTECT_MINUTES)


async def _pending_perk_curse(session_id: str, user: str) -> bool:
    """Whether the previous stream's loser curse still waits for the player's first throw."""
    perk = await get_perk(session_id, user, Perk.CURSE)
    if perk is None or perk.consumed:
        return False
    return perk.active_until is None or perk.active_until > time.time()


async def _cleanse_protection_left(session_id: str, target: str) -> int | None:
    """Minutes a cleansed player stays safe from a new curse. None – no protection."""
    elapsed = await seconds_since_action(session_id, Action.CLEANSE, target, Status.OK, time.time())
    if elapsed is None:
        return None
    return rules.whole_minutes(Rewards.CLEANSE_PROTECT_MINUTES * 60 - elapsed)


# --- flasks ------------------------------------------------------------------

async def _flask_left(session_id: str, user: str) -> int | None:
    """Minutes a swap still holds the player, as its buyer or its partner. None – it does not.

    While it holds, nobody rerolls them and they neither throw nor reroll; a curse still lands.
    """
    holds = await _holds(session_id, user)
    return rules.whole_minutes(max(holds) - time.time()) if holds else None


async def _holds(session_id: str, user: str) -> list[float]:
    """When each swap still holding the player ends, by the bot's clock.

    БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА reaching them after a swap ends that hold for good: a later АГОНИЯ
    taking the blessing back does not bring it back.
    """
    now = time.time()
    blessed_at = await flask_granted_at(session_id, Flask.BLESSING, user, (Status.OK, Status.REVOKED))
    return [ends for drunk_at, ends in await swap_holds(session_id, user, Rewards.FLASK_MINUTES * 60)
            if ends > now and (blessed_at is None or drunk_at > blessed_at)]


async def _flask(session_id: str, actor: str, redemption_id: str, flask: Flask,
                 chatters: Collection[str] | None) -> Outcome:
    if limit := await _stream_limit_reached(session_id, actor, Action.FLASK):
        return Outcome(Status.FLASK_LIMIT, limit=limit)
    # Checked before the draw, for every flask: a refund after it would let a buyer with no
    # roll draw again for free until a flask that needs no roll came up
    row = await get_roll(session_id, actor)
    if row is None:
        return Outcome(Status.NO_ROLL, target=actor)
    # The same for a swap with nobody to swap with: the effect stays hidden
    partners = await _partners(session_id, actor, row)
    if not partners:
        return Outcome(Status.NO_PARTNER, target=actor)
    mode = await _mass(session_id)
    if flask == Flask.SWAP:
        return await _swap(session_id, actor, row, partners)
    if flask == Flask.LUCK:
        return await _luck(session_id, actor, row, mode)
    if flask == Flask.CHOKE:
        return await _choke(session_id, actor, row, mode)
    if flask == Flask.AGONY:
        return await _agony(session_id, actor, row, redemption_id, chatters)
    if flask == Flask.BLESSING:
        return await _blessing(session_id, actor, row, redemption_id, chatters)
    raise ValueError(f'Неизвестная фласка: {flask}')


async def _luck(session_id: str, actor: str, row: RollRow, mode: str | None) -> Outcome:
    """The luck flask: FLASK_MINUTES of lucky own throws. Its state is the journal row itself.

    Each one drunk is a stack of its own minutes, adding its strength (rules.luck_of()). A cursed
    buyer is not refunded – that would be a free redraw: the curse beats the luck until it lifts.
    `ceiling` on the outcome is the curse's, for the text.
    """
    curse = rules.curse_of(row, time.time())
    run = await _luck_run(session_id, actor)
    stacks = (run.stacks if run else 0) + 1
    # A run going on keeps counting its throws: the chance is the next throw's, not the first one's
    throw_no = await count_throws_since(session_id, actor, run.start) + 1 if run else 1
    return Outcome(Status.OK, target=actor, flask=Flask.LUCK, guard_minutes=Rewards.FLASK_MINUTES,
                   luck=round(100 * rules.luck_chance(throw_no, rules.luck_of(mode, stacks))), mode=mode,
                   stacks=stacks, ceiling=curse[0] if curse else None)


class LuckRun(NamedTuple):
    start: float    # when the unbroken run of luck flasks began: the chance counts own throws from it
    end: float      # when the last of them ends
    stacks: int     # how many hold the player now


async def _luck_run(session_id: str, user: str) -> LuckRun | None:
    """The luck flasks holding the player now, None – none does.

    A run is unbroken while each flask was drunk before the ones before it ran out: drinking
    again adds a stack without starting the chance over.
    """
    now, minutes = time.time(), Rewards.FLASK_MINUTES * 60
    drinks = await flask_drinks(session_id, Flask.LUCK, user)
    start = end = None
    for drunk_at in drinks:
        if end is None or drunk_at >= end:
            start = drunk_at
        end = max(end or 0, drunk_at + minutes)
    if end is None or end <= now:
        return None
    stacks = sum(1 for drunk_at in drinks if now - drunk_at < minutes)
    return LuckRun(start, end, stacks)


async def _luck_left(session_id: str, user: str) -> int | None:
    """Minutes the luck flasks still hold the player. None – none does."""
    run = await _luck_run(session_id, user)
    return None if run is None else rules.whole_minutes(run.end - time.time())


async def _partners(session_id: str, actor: str, row: RollRow) -> list[tuple[str, int]]:
    """Who a swap could draw, (name, roll): those it would change something for – not a player a
    shield, a Благодать or another swap holds, and not one with the buyer's own number."""
    now = time.time()
    blocked = (await get_shielded(session_id, now, Rewards.SHIELD_MINUTES * 60)
               | await get_held(session_id, now, Rewards.FLASK_MINUTES * 60))
    return [(name, value) for name, value in await get_players(session_id)
            if name != actor and value != row.value and name not in blocked]


async def _swap(session_id: str, actor: str, row: RollRow, partners: list[tuple[str, int]]) -> Outcome:
    """Swap the buyer's roll with a random one of the partners (_partners(), never empty here).

    A buyer another swap still holds swaps again and is held SWAP_EXTRA_MINUTES longer for every
    such hold.
    """
    extra = Rewards.SWAP_EXTRA_MINUTES * len(await _holds(session_id, actor))
    partner, value = random.choice(partners)
    await set_roll_value(session_id, actor, value)
    await set_roll_value(session_id, partner, row.value)
    standings = await _standings(session_id)
    return Outcome(Status.OK, target=partner, old_value=row.value, value=value, flask=Flask.SWAP,
                   guard_minutes=Rewards.FLASK_MINUTES, extra=extra or None,
                   loser=standings.loser, champion=standings.champion)


async def _choke(session_id: str, actor: str, row: RollRow, mode: str | None) -> Outcome:
    """The choking flask: for FLASK_MINUTES the buyer's roll loses a step every minute from now,
    whatever they throw meanwhile; the step is the one of the moment (rules.choke_step()). Each
    one drunk runs on its own schedule, so two take two steps a minute."""
    now = time.time()
    await add_choke(session_id, actor, now + CHOKE_SECONDS, now + Rewards.FLASK_MINUTES * 60)
    return Outcome(Status.OK, target=actor, value=row.value, flask=Flask.CHOKE,
                   guard_minutes=Rewards.FLASK_MINUTES, mode=mode)


async def _crowd(session_id: str, actor: str, chatters: Collection[str] | None) -> list[str]:
    """Everyone a mass flask reaches, in a random order: who is in chat by Twitch's list or, without
    it, who has written in this stream, plus every player and the buyer; other bots left out.

    The order settles ties: the titles go to the latest of equal rolls, so it must not be
    alphabetical.
    """
    names = set(chatters) if chatters is not None else await get_present(session_id)
    names |= {name for name, _ in await get_players(session_id)}
    names -= Rewards.FLASK_IGNORE
    names.add(actor)
    return random.sample(sorted(names), len(names))


async def _grant(session_id: str, redemption_id: str, action: Action, actor: str, users: list[str],
                 flask: Flask, now: float) -> None:
    """Journal what a mass flask gave each player: the row the shield, the cleanse's ward or the
    curse is read from. The flask's name keeps it out of the buyer's own purchases."""
    await save_grants(session_id, flask, actor, now,
                      [(f'{redemption_id}:{user}', action, user) for user in users])


async def _agony(session_id: str, actor: str, row: RollRow, redemption_id: str,
                 chatters: Collection[str] | None) -> Outcome:
    """АГОНИЯ ЧЕРНОМИРЬЯ: everyone in chat gets a roll from ROLL_MIN to AGONY_TOP and a curse with
    AGONY_CURSE, past every protection; a running curse is doubled (curse_power), not started over.

    It takes back what an earlier БЛАГОСЛОВЕНИЕ handed out. Its curses are journaled with no roll to
    return, so a later cleanse does not undo it. Reads and writes go in batches: the crowd is the
    whole chat list, under the game's lock.
    """
    now = time.time()
    top = rules.agony_top()
    crowd = await _crowd(session_id, actor, chatters)
    await revoke_grants(session_id, Flask.BLESSING, Status.REVOKED)
    rows = await get_rolls(session_id)
    curses = []
    for name in crowd:
        before = rows.get(name)
        if (curse := rules.curse_of(before, now)) is not None:
            until = None if before.curse_until is None else now + 2 * (before.curse_until - now)
            curses.append((name, curse[0], curse[1], until, 2 * (before.curse_power or 1)))
        elif random.random() < Rewards.AGONY_CURSE:
            curses.append((name, Rewards.CURSE_CEILING, None, None, None))
    await save_rolls(session_id, [(name, random.randint(Roll.MIN, top)) for name in crowd])
    await set_curses(session_id, curses)
    await _grant(session_id, redemption_id, Action.CURSE, actor, [name for name, *_ in curses], Flask.AGONY, now)
    return await _mass_outcome(session_id, actor, row, Flask.AGONY, crowd=len(crowd), cursed=len(curses))


async def _blessing(session_id: str, actor: str, row: RollRow, redemption_id: str,
                    chatters: Collection[str] | None) -> Outcome:
    """БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА: everyone in chat gets a roll from BLESS_FLOOR to ROLL_MAX and a
    shield or a cleanse, half and half; the cursed always a cleanse.

    Chokes on them end, a swap's hold too (_holds()). Batched like _agony().
    """
    now = time.time()
    floor = rules.bless_floor()
    crowd = await _crowd(session_id, actor, chatters)
    rows = await get_rolls(session_id)
    waiting = await get_waiting_curses(session_id, now)
    shields, cleanses = [], []
    for name in crowd:
        cursed = rules.curse_of(rows.get(name), now) is not None or name in waiting
        (cleanses if cursed or random.random() >= 0.5 else shields).append(name)
    await save_rolls(session_id, [(name, random.randint(floor, Roll.MAX)) for name in crowd])
    await clear_chokes(session_id, crowd)
    await set_curses(session_id, [(name, None, None, None, None) for name in cleanses])
    await consume_perks(session_id, [name for name in cleanses if name in waiting], Perk.CURSE)
    await _grant(session_id, redemption_id, Action.SHIELD, actor, shields, Flask.BLESSING, now)
    await _grant(session_id, redemption_id, Action.CLEANSE, actor, cleanses, Flask.BLESSING, now)
    return await _mass_outcome(session_id, actor, row, Flask.BLESSING, crowd=len(crowd), shields=len(shields),
                               cleanses=len(cleanses))


async def _mass_outcome(session_id: str, actor: str, row: RollRow, flask: Flask, **counts) -> Outcome:
    """A mass flask's outcome: the buyer's roll before and after it, the titles after it."""
    after = await get_roll(session_id, actor)
    standings = await _standings(session_id)
    return Outcome(Status.OK, target=actor, old_value=row.value, value=after.value, flask=flask,
                   guard_minutes=Rewards.FLASK_MINUTES, loser=standings.loser, champion=standings.champion,
                   **counts)


async def _mass_state(session_id: str) -> tuple[Flask | None, int | None]:
    """The mass flask holding the game and its minutes left; (None, None) – none does.

    The newer of АГОНИЯ and БЛАГОСЛОВЕНИЕ wins: one drunk while the other holds ends it.
    """
    moments = [(at, flask) for flask in (Flask.AGONY, Flask.BLESSING)
               if (at := await flask_last_at(session_id, flask)) is not None]
    if not moments:
        return None, None
    drunk_at, flask = max(moments)
    left = rules.whole_minutes(drunk_at + Rewards.FLASK_MINUTES * 60 - time.time())
    return (flask, left) if left is not None else (None, None)


async def _mass(session_id: str) -> Flask | None:
    """The mass flask holding the game, None – none does."""
    return (await _mass_state(session_id))[0]


async def tick_chokes(session_id: str) -> Standings | None:
    """Take the choking flask's due steps off the rolls. Returns the titles after them, None –
    no step was due.

    Steps a stopped bot missed are taken at once: the schedule runs from the drink, not
    from the last step taken. Each step is the size the mass flask holding the game sets now
    (rules.choke_step()). A roll never drops below ROLL_MIN.
    """
    # Asked every few seconds: with nothing due, the game's lock and a write are not taken
    if not await get_due_chokes(session_id, time.time()):
        return None
    async with _lock, transaction():
        now = time.time()
        due = await get_due_chokes(session_id, now)
        step = rules.choke_step(await _mass(session_id))
        loss: dict[str, int] = {}
        for choke_id, user, next_at, until in due:
            steps = int((min(now, until) - next_at) // CHOKE_SECONDS) + 1
            loss[user] = loss.get(user, 0) + step * steps
            next_at += CHOKE_SECONDS * steps
            # Past its last step the choke is gone
            await move_choke(choke_id, next_at if next_at <= until else None)
        for user, minus in loss.items():
            if (row := await get_roll(session_id, user)) is not None:
                await set_roll_value(session_id, user, max(Roll.MIN, row.value - minus))
        return await _standings(session_id) if due else None

