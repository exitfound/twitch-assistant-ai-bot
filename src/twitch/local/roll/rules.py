"""The rules of the game as pure functions: a throw, a curse, minutes left, a nick.

No database and no clock of their own – the time comes in as `now`, so each rule can be
checked for any moment. game.py applies them under its lock.
"""
import math
import random
import re
from typing import NamedTuple

from src.core.config import Rewards, Roll
from src.core.utils import NICK_TRAILING, clean_nick
from src.twitch.core.viewer import Tier, by_tier
from src.twitch.local.roll.storage import RollRow

# Twitch login: Latin letters, digits and underscore, up to 25 characters
_NICK_RE = re.compile(r'[a-z0-9_]{1,25}')


def throw(ceiling: int | None = None) -> int:
    """A roll from ROLL_MIN up to ROLL_MAX, or up to a curse's ceiling."""
    top = Roll.MAX if ceiling is None else max(Roll.MIN, min(ceiling, Roll.MAX))
    return random.randint(Roll.MIN, top)


class Luck(NamedTuple):
    """The luck flask's own throws: evenly from floor up to just below top, or top itself with a
    chance growing by step percent a throw."""
    floor: int
    top: int
    step: float


def luck_of(mode: str | None, stacks: int = 1) -> Luck:
    """The luck flask's strength: one flask's, times the flasks holding the player, times the mass
    flask holding the game (game.Flask's value, None – none) – half under АГОНИЯ ЧЕРНОМИРЬЯ, which
    also lowers the top to AGONY_LUCK_TOP, double under БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА.

    The chance's step grows with the strength. The floor sits at LUCK_FLOOR's share of the top up
    to a strength of one, and above it closes the gap to the top by that factor: 50 at one, 75 at
    two, 83 at three.
    """
    top = max(Roll.MIN, min(Rewards.AGONY_LUCK_TOP, Roll.MAX)) if mode == 'agony' else Roll.MAX
    strength = stacks * (0.5 if mode == 'agony' else 2 if mode == 'blessing' else 1)
    base = max(Roll.MIN, round(Rewards.LUCK_FLOOR * top / Roll.MAX))
    floor = base if strength <= 1 else round(top - (top - base) / strength)
    return Luck(min(floor, top), top, Rewards.LUCK_STEP * strength)


def luck_chance(throw_no: int, luck: Luck | None = None) -> float:
    """The chance of the top on the n-th own throw under the luck flask: the step per throw,
    counted up to LUCK_THROWS – 2%, 4% … 20% by default, exactly."""
    luck = luck or luck_of(None)
    return min(1.0, luck.step / 100 * min(throw_no, Rewards.LUCK_THROWS))


def lucky_throw(throw_no: int, luck: Luck | None = None) -> int:
    """The n-th own throw under the luck flask: the top with luck_chance(), otherwise evenly from
    the floor up to just below the top."""
    luck = luck or luck_of(None)
    if luck.floor >= luck.top or random.random() < luck_chance(throw_no, luck):
        return luck.top
    return random.randint(luck.floor, luck.top - 1)


def choke_step(mode: str | None) -> int:
    """What the choking flask takes off a roll each minute under the mass flask holding the game:
    twice CHOKE_STEP under АГОНИЯ ЧЕРНОМИРЬЯ, half under БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА, at least 1."""
    if mode == 'agony':
        return Rewards.CHOKE_STEP * 2
    if mode == 'blessing':
        return max(1, Rewards.CHOKE_STEP // 2)
    return Rewards.CHOKE_STEP


def agony_top() -> int:
    """The top of the roll АГОНИЯ ЧЕРНОМИРЬЯ throws everyone, inside the roll's own range."""
    return max(Roll.MIN, min(Rewards.AGONY_TOP, Roll.MAX))


def agony_throw_top() -> int:
    """The top of every throw while АГОНИЯ ЧЕРНОМИРЬЯ holds, inside the roll's own range."""
    return max(Roll.MIN, min(Rewards.AGONY_THROW_TOP, Roll.MAX))


def bless_floor() -> int:
    """The bottom of the roll БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА throws everyone, inside the roll's own range."""
    return max(Roll.MIN, min(Rewards.BLESS_FLOOR, Roll.MAX))


def free_limit(tier: Tier) -> int:
    """Free throws per session by status. The broadcaster's limit is never checked, only
    compared with by the extra roll, so it gets the base one."""
    return by_tier(tier, sub=Roll.FREE_SUB, mod=Roll.FREE_MOD, vip=Roll.FREE_VIP,
                   regular=Roll.FREE_PER_SESSION, broadcaster=Roll.FREE_PER_SESSION)


def parse_nick(raw: str) -> str | None:
    """Nick from the reward input: one word without @ and trailing punctuation.

    Viewers write «@Nick» or «nick,». More than one word, or anything that does not
    look like a login – None.
    """
    words = raw.strip().split()
    if len(words) != 1:
        return None
    nick = clean_nick(words[0])
    # clean_nick() cuts to the login length: a longer word is not a login, not a cut one
    if len(nick) < len(words[0].lstrip('@').rstrip(NICK_TRAILING)):
        return None
    return nick if _NICK_RE.fullmatch(nick) else None


def burst_pause_left(times: list[float], now: float, throws: int, window: int, pause: int) -> float | None:
    """Seconds until a player who threw too fast may throw again, or None if they may now.

    times – the player's last throws, newest first. Too fast is `throws` of them within
    `window` seconds; the pause runs from the newest, so a refused attempt, which is
    not a throw, does not stretch it.
    """
    if len(times) < throws or times[0] - times[throws - 1] > window:
        return None
    left = pause - (now - times[0])
    return left if left > 0 else None


def window_left(times: list[float], now: float, limit: int, window: int) -> float | None:
    """Seconds until a buyer who used up their window may buy again, or None if they may now.

    times – their past purchases, oldest first. A window opens with the first purchase
    and lasts `window` seconds; the first purchase after it opens the next one. Only
    `limit` fit into a window.
    """
    start, count = None, 0
    for moment in times:
        if start is None or moment - start >= window:
            start, count = moment, 0
        count += 1
    if start is None or count < limit:
        return None
    left = window - (now - start)
    return left if left > 0 else None


def whole_minutes(seconds: float) -> int | None:
    """Seconds left as whole minutes, rounded up: «1 minute» to the very end. None – nothing left."""
    return max(1, math.ceil(seconds / 60)) if seconds > 0 else None


def curse_of(row: RollRow | None, now: float) -> tuple[int, float | None] | None:
    """The active curse: (ceiling of the next throw, when it reached the floor).

    The curse does not need lifting separately: once the ceiling has sat on the
    floor longer than its hold (curse_hold()), the row simply stops counting as cursed.
    A new session is a new row with no curse in it. The curse of the previous
    stream's loser also has a hard deadline, curse_until.
    """
    if row is None or row.curse_ceiling is None:
        return None
    if row.curse_until is not None and now >= row.curse_until:
        return None
    floor_at = row.curse_floor_at
    if floor_at is not None and now - floor_at >= curse_hold(row.curse_power):
        return None
    return row.curse_ceiling, floor_at


def curse_hold(power: int | None) -> float:
    """Seconds a curse holds on the floor: CURSE_HOLD_MINUTES, times the doubling АГОНИЯ ЧЕРНОМИРЬЯ gave it."""
    return Rewards.CURSE_HOLD_MINUTES * 60 * (power or 1)


def curse_step(power: int | None) -> int:
    """How far a throw lowers a curse's ceiling: CURSE_STEP, divided by its doubling, at least 1."""
    return max(1, Rewards.CURSE_STEP // (power or 1))


def lowered(ceiling: int, floor_at: float | None, now: float, power: int | None = None) -> tuple[int, float | None]:
    """The ceiling after a cursed player's throw: one step lower, but not below the floor.

    The countdown to the lift starts the moment the ceiling first reaches the
    floor, and does not move after that.
    """
    next_ceiling = max(Rewards.CURSE_FLOOR, ceiling - curse_step(power))
    if floor_at is None and next_ceiling == Rewards.CURSE_FLOOR:
        floor_at = now
    return next_ceiling, floor_at


def minutes_left(floor_at: float | None, until: float | None, now: float, power: int | None = None) -> int | None:
    """Minutes until a curse sitting on the floor lifts. None – nothing to wait for.

    None means either that the ceiling is still dropping (no floor_at yet) or that the
    hold has already run out. texts.curse_note() picks a different line by exactly that,
    so «expired» must not come back as «one minute left». The previous stream's loser
    curse also has a hard deadline, until: the lift comes at whichever is earlier.
    """
    if floor_at is None:
        return None
    lifts_at = floor_at + curse_hold(power)
    if until is not None:
        lifts_at = min(lifts_at, until)
    return whole_minutes(lifts_at - now)

