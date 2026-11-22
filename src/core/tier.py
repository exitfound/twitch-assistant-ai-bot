"""The viewer status ladder: one place every per-status number reads.

Platform-free: a service gets the tier with its request, the platform turns badges into it.
"""
from enum import StrEnum
from typing import TypeVar

from src.core.content import Content

T = TypeVar('T')


class Tier(StrEnum):
    BROADCASTER = 'broadcaster'
    MODERATOR = 'moderator'
    SUB = 'subscriber'
    VIP = 'vip'
    REGULAR = 'regular'


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


def tier_sub_hint(tier: Tier, limit: int, sub_limit: int) -> str:
    """The subscription hint appended to a refusal by limit, or '' when a subscription
    would not raise it: a subscriber or the broadcaster, or a subscriber's sub_limit
    no higher than this limit. 0 – no limit."""
    if tier in (Tier.SUB, Tier.BROADCASTER) or (sub_limit and limit >= sub_limit):
        return ''
    return ' ' + Content.text('sub_hint')
