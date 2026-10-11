"""The viewer status ladder: one place that turns chat badges into a tier.

Cooldowns, quotas, free throws and per-stream limits all read it.
"""
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

from src.core.content import Content

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
    """Broadcaster, subscriber, moderator, VIP, everyone else – in that order.

    A subscription is checked before the moderator and VIP badges, so someone with both
    gets the subscriber's terms: the widest limits come only with a subscription.
    Channel founders wear their own badge instead of the subscriber one.
    """
    if chatter.broadcaster:
        return Tier.BROADCASTER
    if chatter.subscriber or chatter.founder:
        return Tier.SUB
    if chatter.moderator:
        return Tier.MODERATOR
    if chatter.vip:
        return Tier.VIP
    return Tier.REGULAR


def by_tier(tier: Tier, *, sub: T, mod: T, vip: T, regular: T, broadcaster: T) -> T:
    """Pick a value by tier."""
    if tier == Tier.BROADCASTER:
        return broadcaster
    if tier == Tier.SUB:
        return sub
    if tier == Tier.MODERATOR:
        return mod
    if tier == Tier.VIP:
        return vip
    return regular


def sub_hint(chatter: 'twitchio.Chatter', limit: int, sub_limit: int) -> str:
    """The subscription hint appended to a refusal by limit, or '' when a subscription
    would not raise it: a subscriber or the broadcaster, or a subscriber's sub_limit
    no higher than this limit. 0 – no limit."""
    return tier_sub_hint(tier_of(chatter), limit, sub_limit)


def tier_sub_hint(tier: Tier, limit: int, sub_limit: int) -> str:
    """sub_hint() by a stored status, for a reward redemption, which carries no badges."""
    if tier in (Tier.SUB, Tier.BROADCASTER) or (sub_limit and limit >= sub_limit):
        return ''
    return ' ' + Content.text('sub_hint')
