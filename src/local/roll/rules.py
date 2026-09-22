"""The rules of the game as pure functions: a throw, a curse, minutes left, a nick.

No database and no clock of their own – the time comes in as `now`, so each rule can be
checked for any moment. game.py applies them under its lock.
"""
import math
import random
import re

from src.core.config import Rewards, Roll
from src.core.utils import NICK_TRAILING, clean_nick
from src.local.roll.storage import RollRow

# Twitch login: Latin letters, digits and underscore, up to 25 characters
_NICK_RE = re.compile(r'[a-z0-9_]{1,25}')


def throw(ceiling: int | None = None) -> int:
    """A roll from ROLL_MIN up to ROLL_MAX, or up to a curse's ceiling."""
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
    nick = clean_nick(words[0])
    # clean_nick() cuts to the login length: a longer word is not a login, not a cut one
    if len(nick) < len(words[0].lstrip('@').rstrip(NICK_TRAILING)):
        return None
    return nick if _NICK_RE.fullmatch(nick) else None


def whole_minutes(seconds: float) -> int | None:
    """Seconds left as whole minutes, rounded up: «1 minute» to the very end. None – nothing left."""
    return max(1, math.ceil(seconds / 60)) if seconds > 0 else None


def curse_of(row: RollRow | None, now: float) -> tuple[int, float | None] | None:
    """The active curse: (ceiling of the next throw, when it reached the floor).

    The curse does not need lifting separately: once the ceiling has sat on the
    floor longer than CURSE_HOLD_MINUTES, the row simply stops counting as cursed.
    A new session is a new row with no curse in it. The curse of the previous
    stream's loser also has a hard deadline, curse_until.
    """
    if row is None or row.curse_ceiling is None:
        return None
    if row.curse_until is not None and now >= row.curse_until:
        return None
    floor_at = row.curse_floor_at
    if floor_at is not None and now - floor_at >= Rewards.CURSE_HOLD_MINUTES * 60:
        return None
    return row.curse_ceiling, floor_at


def lowered(ceiling: int, floor_at: float | None, now: float) -> tuple[int, float | None]:
    """The ceiling after a cursed player's throw: one step lower, but not below the floor.

    The countdown to the lift starts the moment the ceiling first reaches the
    floor, and does not move after that.
    """
    next_ceiling = max(Rewards.CURSE_FLOOR, ceiling - Rewards.CURSE_STEP)
    if floor_at is None and next_ceiling == Rewards.CURSE_FLOOR:
        floor_at = now
    return next_ceiling, floor_at


def minutes_left(floor_at: float | None, until: float | None, now: float) -> int | None:
    """Minutes until a curse sitting on the floor lifts. None – nothing to wait for.

    None means either that the ceiling is still dropping (no floor_at yet) or that the
    hold has already run out. texts.curse_note() picks a different line by exactly that,
    so «expired» must not come back as «one minute left». The previous stream's loser
    curse also has a hard deadline, until: the lift comes at whichever is earlier.
    """
    if floor_at is None:
        return None
    lifts_at = floor_at + Rewards.CURSE_HOLD_MINUTES * 60
    if until is not None:
        lifts_at = min(lifts_at, until)
    return whole_minutes(lifts_at - now)


def visible_champion(loser: tuple[str, int] | None,
                     champion: tuple[str, int] | None) -> tuple[str, int] | None:
    """The champion to show: none when it is the loser too – one player, or everyone
    threw the same number, and there is nobody to praise them against."""
    if champion is not None and loser is not None and champion[0] == loser[0]:
        return None
    return champion
