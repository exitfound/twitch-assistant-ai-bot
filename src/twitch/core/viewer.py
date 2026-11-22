"""Chat badges → the status tier (src/core/tier.py). Cooldowns, quotas and limits read it."""
from typing import TYPE_CHECKING

from src.core.tier import Tier, by_tier, tier_sub_hint

if TYPE_CHECKING:
    import twitchio

__all__ = ['Tier', 'by_tier', 'sub_hint', 'tier_of']


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


def sub_hint(chatter: 'twitchio.Chatter', limit: int, sub_limit: int) -> str:
    """tier_sub_hint() by the chatter's badges."""
    return tier_sub_hint(tier_of(chatter), limit, sub_limit)
