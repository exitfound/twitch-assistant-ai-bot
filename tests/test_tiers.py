"""The viewer status ladder, implemented in several places that must agree.

Broadcaster above everyone, moderator and subscriber (founder included) together,
then VIP, then everyone else; a subscribing VIP gets the subscriber's terms.
"""
import pytest

from fakes import make_chatter
from src.core import component
from src.core.commands import Role
from src.core.config import Cooldown, Picture, Quota, Roll, Summary, Who
from src.core.viewer import Tier, tier_of
from src.gemini import commands as gemini_commands
from src.gemini import summary, who
from src.gemini.picture import command as picture_command
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


@pytest.mark.parametrize(('who_', 'sub_role'), [
    ('broadcaster', True),
    ('moderator', True),
    ('vip', True),
    ('subscriber', True),
    ('founder', True),
    ('sub_vip', True),
    ('regular', False),
])
def test_roles_look_at_badges(who_, sub_role):
    chatter = make_chatter(**BADGES[who_])
    assert component._has_role(None, chatter)
    assert component._has_role(Role.SUB_VIP_MOD_BROADCASTER, chatter) is sub_role


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


@pytest.mark.parametrize(('kind', 'limits'), [
    (who.WHO_KIND, (Who.PER_STREAM_FOLLOWER, Who.PER_STREAM_VIP, Who.PER_STREAM_SUB)),
    (who.VERSUS_KIND, (Who.PER_STREAM_FOLLOWER, Who.PER_STREAM_VIP, Who.PER_STREAM_SUB)),
    (summary.KIND, (Summary.PER_STREAM_FOLLOWER, Summary.PER_STREAM_VIP, Summary.PER_STREAM_SUB)),
])
def test_per_stream_limits(kind, limits):
    follower, vip, sub = limits
    limit = gemini_commands._limit_for
    assert limit(kind, make_chatter(broadcaster=True)) == 0
    assert limit(kind, make_chatter(moderator=True)) == sub
    assert limit(kind, make_chatter(subscriber=True, vip=True)) == sub
    assert limit(kind, make_chatter(vip=True)) == vip
    assert limit(kind, make_chatter()) == follower


def test_picture_limits():
    limit = picture_command._limit_for
    assert limit(make_chatter(broadcaster=True)) == 0
    assert limit(make_chatter(founder=True)) == Picture.PER_STREAM_SUB
    assert limit(make_chatter(vip=True)) == Picture.PER_STREAM_VIP
