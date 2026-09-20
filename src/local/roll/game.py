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
import math
import random
import re
import time

from src.core.config import Rewards, Roll
from src.core.database import get_previous_stream_session, get_session_start, has_chatted
from src.local.roll.storage import (
    RollRow, activate_perks, add_perk, consume_perk, get_action_status, get_expired_curses,
    get_last_roll_session_before, get_perk, get_roll, get_session_champion, get_session_loser,
    has_action, save_action, save_roll, seconds_since_action, set_curse,
)

# Actions bought with channel points
ACTION_EXTRA = 'extra'      # a throw for yourself beyond the free ones
ACTION_REROLL = 'reroll'    # a throw for someone else, the shield protects from it
ACTION_CURSE = 'curse'      # a capped throw for another, then the cap drops; pierces the shield
ACTION_SHIELD = 'shield'    # protection from rerolls until the session ends
ACTIONS = (ACTION_EXTRA, ACTION_REROLL, ACTION_CURSE, ACTION_SHIELD)

# Perks from the previous stream: not bought, granted at the start of the new one
PERK_SHIELD = 'shield'      # to the китежанин (champion) – a shield from rerolls
PERK_CURSE = 'curse'        # to the залупа (loser) – a curse with a hard deadline

# Outcomes. OK – the change is applied, everything else is a refusal with a reason
OK = 'ok'
NO_FREE_LEFT = 'no_free_left'           # !roll: free throws are used up
FREE_LEFT = 'free_left'                 # extra roll bought while free throws are still left
BAD_TARGET = 'bad_target'               # the reward input is not a nick
SELF_TARGET = 'self_target'             # rerolling or cursing yourself
NOT_ROLLED = 'not_rolled'               # the target has not rolled today
SHIELDED = 'shielded'                   # the target has a shield
ALREADY_SHIELDED = 'already_shielded'   # shield bought a second time
ALREADY_CURSED = 'already_cursed'       # the target is already cursed
PROTECTED = 'protected'                 # the target was rerolled recently, protection still holds
UNKNOWN_TARGET = 'unknown_target'       # reroll for a nick seen neither in the game nor in chat
PERK_SHIELDED = 'perk_shielded'         # the target has the previous stream's champion shield
DUPLICATE = 'duplicate'                 # Twitch sent the same redemption again

# Twitch login: Latin letters, digits and underscore, up to 25 characters
_NICK_RE = re.compile(r'[a-z0-9_]{1,25}')

_lock = asyncio.Lock()


@dataclasses.dataclass(frozen=True)
class Outcome:
    status: str
    target: str | None = None               # whose roll is affected
    old_value: int | None = None            # roll before the operation
    value: int | None = None                # roll after the operation
    free_left: int | None = None            # free throws left, None – no limit
    loser: tuple[str, int] | None = None    # session loser after the operation
    champion: tuple[str, int] | None = None # session champion, None – if also the loser
    # The target's curse, if it applied to this throw
    ceiling: int | None = None              # ceiling of this throw
    next_ceiling: int | None = None         # ceiling of the next one
    curse_minutes_left: int | None = None   # ceiling on the floor: minutes until it lifts
    protect_minutes_left: int | None = None # protection after a reroll: minutes left

    @property
    def ok(self) -> bool:
        return self.status == OK


def _throw(ceiling: int | None = None) -> int:
    top = Roll.MAX if ceiling is None else max(Roll.MIN, min(ceiling, Roll.MAX))
    return random.randint(Roll.MIN, top)


def parse_nick(raw: str) -> str | None:
    """Nick from the reward input: the first word without @ and trailing punctuation.

    Viewers write «@Nick», «nick,» or «ник и ещё что-то». Everything after the
    first word is dropped; anything that does not look like a login – None.
    """
    words = raw.strip().split()
    if not words:
        return None
    nick = words[0].lstrip('@').rstrip(',.:;!?').lower()
    return nick if _NICK_RE.fullmatch(nick) else None


# --- curse -------------------------------------------------------------------

def _curse_of(row: RollRow | None) -> tuple[int, float | None] | None:
    """The active curse: (ceiling of the next throw, when it reached the floor).

    The curse does not need lifting separately: once the ceiling has sat on the
    floor longer than CURSE_HOLD_MINUTES, the row simply stops counting as cursed.
    A new session is a new row with no curse in it. The curse of the previous
    stream's loser also has a hard deadline, curse_until.
    """
    if row is None or row.curse_ceiling is None:
        return None
    if row.curse_until is not None and time.time() >= row.curse_until:
        return None
    floor_at = row.curse_floor_at
    if floor_at is not None and time.time() - floor_at >= Rewards.CURSE_HOLD_MINUTES * 60:
        return None
    return row.curse_ceiling, floor_at


def _lowered(ceiling: int, floor_at: float | None) -> tuple[int, float | None]:
    """The ceiling after a cursed player's throw: one step lower, but not below the floor.

    The countdown to the lift starts the moment the ceiling first reaches the
    floor, and does not move after that.
    """
    next_ceiling = max(Rewards.CURSE_FLOOR, ceiling - Rewards.CURSE_STEP)
    if floor_at is None and next_ceiling == Rewards.CURSE_FLOOR:
        floor_at = time.time()
    return next_ceiling, floor_at


def _minutes_left(floor_at: float | None) -> int | None:
    """Minutes until a curse sitting on the floor lifts. None – nothing to wait for.

    None means either that the ceiling is still dropping (no floor_at yet) or that the
    hold has already run out. texts.curse_note() picks a different line by exactly that,
    so «expired» must not come back as «one minute left» (2026-09-20).
    """
    if floor_at is None:
        return None
    seconds = Rewards.CURSE_HOLD_MINUTES * 60 - (time.time() - floor_at)
    return max(1, math.ceil(seconds / 60)) if seconds > 0 else None


async def _throw_for(
    session_id: str, user: str, row: RollRow | None, *, free_throw: bool, limit: int | None = None,
) -> dict:
    """A throw on a player's row – their own or someone else's reroll.

    A cursed player's throw is capped by the ceiling, and the ceiling drops a step:
    any throw on the victim, whoever made it, brings the curse closer to the floor.
    """
    curse = _curse_of(row)
    until = row.curse_until if curse is not None else None
    if curse is None:
        # The previous stream's loser curse is laid on the first throw on them
        until = await _take_perk_curse(session_id, user)
        if until is not None:
            curse = (Rewards.CURSE_CEILING, None)
    if curse is None:
        value = _throw()
        await save_roll(session_id, user, value, free_throw=free_throw, limit=limit)
        return {'value': value}
    ceiling, floor_at = curse
    value = _throw(ceiling)
    await save_roll(session_id, user, value, free_throw=free_throw, limit=limit)
    next_ceiling, floor_at = _lowered(ceiling, floor_at)
    await set_curse(session_id, user, next_ceiling, floor_at, until)
    return {
        'value': value, 'ceiling': ceiling, 'next_ceiling': next_ceiling,
        'curse_minutes_left': _minutes_left(floor_at),
    }


async def _standings(session_id: str) -> dict:
    """The session loser (залупа) and champion (китежанин) after a throw.

    The champion is not shown if it is the same person as the loser: that happens
    when only one player rolled or everyone threw the same number – there is
    nobody to praise them against.
    """
    loser = await get_session_loser(session_id)
    champion = await get_session_champion(session_id)
    if champion is not None and loser is not None and champion[0] == loser[0]:
        champion = None
    return {'loser': loser, 'champion': champion}


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
    perk = await get_perk(session_id, user, PERK_SHIELD)
    if perk is None or perk.active_until is None:
        return None
    seconds = perk.active_until - time.time()
    return max(1, math.ceil(seconds / 60)) if seconds > 0 else None


async def _take_perk_curse(session_id: str, user: str) -> float | None:
    """Take the loser's curse for the first throw on them. Returns its deadline.

    There is one curse: once laid on a throw it is not given a second time, even
    if it was lifted early. A player who did not show up in time loses it.
    """
    await _activate_perks(session_id, user)
    perk = await get_perk(session_id, user, PERK_CURSE)
    if perk is None or perk.consumed or perk.active_until is None or perk.active_until <= time.time():
        return None
    await consume_perk(session_id, user, PERK_CURSE)
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


async def grant_perks(session_id: str) -> tuple[str | None, str | None] | None:
    """Grant the perks from the previous stream: (champion, loser).

    None – nothing to grant or everything is already granted. Safe to repeat after
    a restart or a stream outage: nothing is granted a second time.
    """
    async with _lock:
        previous = await _previous_session(session_id)
        if previous is None:
            return None
        loser = await get_session_loser(previous)
        champion = await get_session_champion(previous)
        if champion is not None and loser is not None and champion[0] == loser[0]:
            champion = None
        shielded = champion is not None and await add_perk(session_id, champion[0], PERK_SHIELD, previous)
        cursed = loser is not None and await add_perk(session_id, loser[0], PERK_CURSE, previous)
    if not (shielded or cursed):
        return None
    return (champion[0] if shielded else None, loser[0] if cursed else None)


async def appear(session_id: str, user: str) -> list[str]:
    """A player showed up in the stream chat: start their perk countdown. Returns which started."""
    async with _lock:
        return await _activate_perks(session_id, user)


# --- operations --------------------------------------------------------------

async def free_throw(
    session_id: str, user: str, *, limit: int, unlimited: bool = False,
) -> Outcome:
    """A free !roll: no more than limit per session.

    limit depends on the viewer's status (see free_limit_for() in the command) and
    is stored in the player's row: a reward redemption arrives without badges.
    unlimited – no limit applies (broadcaster). The throw is still counted, so the
    paid extra roll works for them like for everyone. free_left of such an outcome
    is None: there is no remainder to mention.
    """
    async with _lock:
        row = await get_roll(session_id, user)
        used = row.free_throws if row else 0
        if not unlimited and used >= limit:
            return Outcome(NO_FREE_LEFT, target=user, free_left=0)
        throw = await _throw_for(session_id, user, row, free_throw=True, limit=limit)
        standings = await _standings(session_id)
    return Outcome(
        OK, target=user, old_value=row.value if row else None,
        free_left=None if unlimited else max(0, limit - used - 1),
        **standings, **throw,
    )


@dataclasses.dataclass(frozen=True)
class Standing:
    """What !rollstat shows: the player's own state plus the session's two titles."""
    value: int | None = None                # their roll, None – they have not rolled
    free_left: int | None = None            # free throws left, None – no limit
    ceiling: int | None = None              # curse: ceiling of their next throw
    curse_minutes_left: int | None = None   # ceiling on the floor: minutes until it lifts
    shield: bool = False                    # a shield bought with points, holds till the session ends
    shield_minutes_left: int | None = None  # the previous stream's champion shield
    loser: tuple[str, int] | None = None
    champion: tuple[str, int] | None = None


async def status(session_id: str, user: str, *, limit: int, unlimited: bool = False) -> Standing:
    """The player's standing in the session. Reads only: no throw, no perk started.

    _perk_shield_left() is not used on purpose – it activates the perk, and merely
    asking about your state must not start the countdown.
    """
    row = await get_roll(session_id, user)
    curse = _curse_of(row)
    perk = await get_perk(session_id, user, PERK_SHIELD)
    left = None
    if perk is not None and not perk.consumed and perk.active_until is not None:
        seconds = perk.active_until - time.time()
        left = max(1, math.ceil(seconds / 60)) if seconds > 0 else None
    return Standing(
        value=row.value if row else None,
        free_left=None if unlimited else max(0, limit - (row.free_throws if row else 0)),
        ceiling=curse[0] if curse else None,
        curse_minutes_left=_minutes_left(curse[1]) if curse else None,
        shield=await has_action(session_id, ACTION_SHIELD, user, OK),
        shield_minutes_left=left,
        **await _standings(session_id),
    )


async def lift_expired_curses(session_id: str) -> list[str]:
    """Lift expired curses: the ceiling sat on the floor longer than CURSE_HOLD_MINUTES
    or the hard deadline of the previous stream's loser curse passed.

    The mechanics do not need the lift: _curse_of() already stops treating such a
    row as cursed. It is there so the bot says so in chat exactly once – a cleared
    row will not be selected again. Under the shared lock, so as not to erase a
    curse laid anew between the select and the write.
    """
    async with _lock:
        now = time.time()
        users = await get_expired_curses(session_id, now - Rewards.CURSE_HOLD_MINUTES * 60, now)
        for user in users:
            await set_curse(session_id, user, None, None)
    return users


async def redeem(
    action: str, session_id: str, actor: str, user_input: str, redemption_id: str,
) -> Outcome:
    """Apply a bought reward and record the outcome in the roll_actions journal.

    The journal also guards against duplicates: Twitch may send one event twice,
    and the second one must not throw. The check and the write run under the same
    lock as the throw itself, so a duplicate cannot slip in between them.
    """
    async with _lock:
        if await get_action_status(redemption_id) is not None:
            return Outcome(DUPLICATE)
        if action == ACTION_EXTRA:
            outcome = await _extra(session_id, actor)
        elif action == ACTION_SHIELD:
            outcome = await _shield(session_id, actor)
        elif action == ACTION_REROLL:
            outcome = await _reroll(session_id, actor, user_input)
        elif action == ACTION_CURSE:
            outcome = await _curse(session_id, actor, user_input)
        else:
            raise ValueError(f'Неизвестное действие: {action}')
        await save_action(
            redemption_id, session_id, action, actor, user_input,
            outcome.target, outcome.old_value, outcome.value, outcome.status,
        )
    return outcome


async def _extra(session_id: str, actor: str) -> Outcome:
    row = await get_roll(session_id, actor)
    used = row.free_throws if row else 0
    # A redemption event carries no badges, so the limit comes from the player's row –
    # written by their own throw from chat. Never rolled – the base limit
    limit = (row.free_limit if row and row.free_limit else None) or Roll.FREE_PER_SESSION
    if used < limit:
        # Points for a throw that is free anyway are almost certainly a misclick
        return Outcome(FREE_LEFT, target=actor, free_left=limit - used)
    throw = await _throw_for(session_id, actor, row, free_throw=False)
    return Outcome(
        OK, target=actor, old_value=row.value if row else None,
        **await _standings(session_id), **throw,
    )


async def _shield(session_id: str, actor: str) -> Outcome:
    # The shield itself is a successful journal row, it has no state of its own
    if await has_action(session_id, ACTION_SHIELD, actor, OK):
        return Outcome(ALREADY_SHIELDED, target=actor)
    return Outcome(OK, target=actor)


async def _target(
    session_id: str, actor: str, user_input: str, *, require_roll: bool,
) -> tuple[str, RollRow | None] | Outcome:
    """The target of a reroll or curse – or a refusal if there is nobody to touch.

    require_roll – the target must have rolled this session: a curse is laid only
    on someone already in the game. A reroll also throws for someone who has not
    rolled yet, but only for a nick that has written in chat at least once:
    otherwise a typo in the reward input would create a roll for a nonexistent
    player, who could become the «залупа стрима».
    """
    target = parse_nick(user_input)
    if target is None:
        return Outcome(BAD_TARGET)
    if target == actor:
        return Outcome(SELF_TARGET, target=target)
    row = await get_roll(session_id, target)
    if row is None:
        if require_roll:
            return Outcome(NOT_ROLLED, target=target)
        if not await has_chatted(target):
            return Outcome(UNKNOWN_TARGET, target=target)
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
    elapsed = await seconds_since_action(session_id, ACTION_REROLL, target, OK)
    if elapsed is None or elapsed >= window:
        return None
    return max(1, math.ceil((window - elapsed) / 60))


async def _reroll(session_id: str, actor: str, user_input: str) -> Outcome:
    found = await _target(session_id, actor, user_input, require_roll=False)
    if isinstance(found, Outcome):
        return found
    target, row = found
    if await has_action(session_id, ACTION_SHIELD, target, OK):
        return Outcome(SHIELDED, target=target)
    perk_left = await _perk_shield_left(session_id, target)
    if perk_left is not None:
        return Outcome(PERK_SHIELDED, target=target, protect_minutes_left=perk_left)
    protect_left = await _protection_left(session_id, target)
    if protect_left is not None:
        return Outcome(PROTECTED, target=target, protect_minutes_left=protect_left)
    # On a cursed target the reroll is capped by their ceiling and lowers it the same
    # way the victim's own throw does. The victim's free throws are not spent
    throw = await _throw_for(session_id, target, row, free_throw=False)
    return Outcome(
        OK, target=target, old_value=row.value if row else None,
        **await _standings(session_id), **throw,
    )


async def _curse(session_id: str, actor: str, user_input: str) -> Outcome:
    found = await _target(session_id, actor, user_input, require_roll=True)
    if isinstance(found, Outcome):
        return found
    target, row = found
    # The shield is deliberately not checked: the curse pierces it, that is what sets
    # it apart from the cheap reroll
    if _curse_of(row) is not None:
        # A new curse would reset the ceiling to the top – a gift to the victim
        return Outcome(ALREADY_CURSED, target=target)
    ceiling = Rewards.CURSE_CEILING
    value = _throw(ceiling)
    await save_roll(session_id, target, value, free_throw=False)
    next_ceiling, floor_at = _lowered(ceiling, None)
    await set_curse(session_id, target, next_ceiling, floor_at)
    return Outcome(
        OK, target=target, old_value=row.value, value=value,
        ceiling=ceiling, next_ceiling=next_ceiling, curse_minutes_left=_minutes_left(floor_at),
        **await _standings(session_id),
    )
