"""The viewer status ladder, implemented in several places that must agree.

Broadcaster above everyone, moderator and subscriber (founder included) together,
then VIP, then everyone else; a subscribing VIP gets the subscriber's terms.
"""
import pytest

from fakes import make_chatter
from src.core import component
from src.core.config import Cooldown, PerStream, Quota, Roll
from src.core.limits import limit_for
from src.core.viewer import Tier, sub_hint, tier_of
from src.local.roll.command import free_limit_for

BADGES = {
    'broadcaster': {'broadcaster': True},
    'moderator': {'moderator': True},
    'subscriber': {'subscriber': True},
    'founder': {'founder': True},
    'sub_vip': {'subscriber': True, 'vip': True},
    'vip': {'vip': True},
    'regular': {},
}

TIER = {
    'broadcaster': Tier.BROADCASTER,
    'moderator': Tier.MODERATOR,
    'subscriber': Tier.SUB,
    'founder': Tier.SUB,
    'sub_vip': Tier.SUB,
    'vip': Tier.VIP,
    'regular': Tier.REGULAR,
}


@pytest.mark.parametrize('who_', BADGES)
def test_tier(who_):
    assert tier_of(make_chatter(**BADGES[who_])) == TIER[who_]


@pytest.mark.parametrize(('who_', 'cooldown', 'quota'), [
    ('broadcaster', 0, 0),
    ('moderator', 0, 0),
    ('subscriber', 0, 0),
    ('vip', Cooldown.VIP, Quota.VIP_PER_HOUR),
    ('regular', Cooldown.REGULAR, Quota.FOLLOWER_PER_HOUR),
])
def test_cooldown_and_quota(who_, cooldown, quota):
    tier = TIER[who_]
    assert component._cooldown_seconds(tier) == cooldown
    assert component._quota_per_hour(tier) == quota


@pytest.mark.parametrize(('who_', 'limit'), [
    ('broadcaster', Roll.FREE_PER_SESSION),
    ('moderator', Roll.FREE_SUB),
    ('subscriber', Roll.FREE_SUB),
    ('founder', Roll.FREE_SUB),
    ('sub_vip', Roll.FREE_SUB),
    ('vip', Roll.FREE_VIP),
    ('regular', Roll.FREE_PER_SESSION),
])
def test_free_throws(who_, limit):
    assert free_limit_for(make_chatter(**BADGES[who_])) == limit


@pytest.mark.parametrize(('who_', 'limit'), [
    ('broadcaster', 0),
    ('moderator', PerStream.SUB),
    ('subscriber', PerStream.SUB),
    ('founder', PerStream.SUB),
    ('sub_vip', PerStream.SUB),
    ('vip', PerStream.VIP),
    ('regular', PerStream.FOLLOWER),
])
def test_per_stream_limits(who_, limit):
    assert limit_for(make_chatter(**BADGES[who_])) == limit


@pytest.mark.parametrize(('who_', 'hinted'), [
    ('broadcaster', False),
    ('moderator', False),
    ('subscriber', False),
    ('founder', False),
    ('sub_vip', False),
    ('vip', True),
    ('regular', True),
])
def test_sub_hint_only_where_a_subscription_helps(who_, hinted):
    assert bool(sub_hint(make_chatter(**BADGES[who_]))) is hinted
