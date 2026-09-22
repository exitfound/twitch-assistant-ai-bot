"""The viewer status ladder: one place that turns chat badges into a tier.

Cooldowns, quotas, free throws and per-stream limits all read it. Roles are not
here: a command's role looks at the badges themselves (see _has_role() in
component.py), because a tier folds subscriber and VIP together.
"""
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    import twitchio

T = TypeVar('T')


class Tier(StrEnum):
    BROADCASTER = 'broadcaster'
    MODERATOR = 'moderator'
    SUB = 'subscriber'
    VIP = 'vip'
    REGULAR = 'regular'


def tier_of(chatter: 'twitchio.Chatter') -> Tier:
    """Broadcaster, moderator, subscriber, VIP, everyone else – in that order.

    Sub is checked before VIP, so someone who is both gets the gentler terms.
    Channel founders wear their own badge instead of the subscriber one.
    """
    if chatter.broadcaster:
        return Tier.BROADCASTER
    if chatter.moderator:
        return Tier.MODERATOR
    if chatter.subscriber or chatter.founder:
        return Tier.SUB
    if chatter.vip:
        return Tier.VIP
    return Tier.REGULAR


def by_tier(tier: Tier, *, sub: T, vip: T, regular: T, broadcaster: T) -> T:
    """Pick a value by tier. A moderator gets the subscriber's value."""
    if tier == Tier.BROADCASTER:
        return broadcaster
    if tier in (Tier.MODERATOR, Tier.SUB):
        return sub
    if tier == Tier.VIP:
        return vip
    return regular
