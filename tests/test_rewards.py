"""The Twitch side of the channel-points rewards, against a fake broadcaster."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.local.roll.rewards import RewardService
from src.local.roll.storage import get_reward_ids


class FakeBroadcaster:
    def __init__(self) -> None:
        self.paused: list[bool] = []
        self.fail_pause = False
        self.settle_asked = False

    async def fetch_custom_rewards(self, *, ids=None, manageable=True):
        if ids is not None:
            self.settle_asked = True
        return []

    async def create_custom_reward(self, title, cost, **kwargs):
        return SimpleNamespace(id=f'id-{title}')

    async def update_custom_reward(self, reward_id, **changes):
        if 'paused' in changes:
            if self.fail_pause:
                raise RuntimeError('twitch 500')
            self.paused.append(changes['paused'])
        return SimpleNamespace(id=reward_id)


@pytest.fixture
def twitch():
    broadcaster = FakeBroadcaster()
    bot = SimpleNamespace(
        create_partialuser=lambda channel_id: broadcaster,
        subscribe_websocket=AsyncMock(),
    )
    return RewardService(bot), bot, broadcaster


async def test_start_creates_the_rewards_and_opens_them_live(db, twitch):
    service, _, broadcaster = twitch
    await service.start('chan', open_=True)
    assert service.active
    assert len(await get_reward_ids()) == 5
    assert set(broadcaster.paused) == {False}


async def test_stream_going_live_during_start_is_not_lost(db, twitch):
    """start() takes several requests; a stream.online arriving meanwhile must win over
    the offline state start() was called with."""
    service, bot, broadcaster = twitch

    async def online_meanwhile(*args, **kwargs):
        await service.set_open(True)
    bot.subscribe_websocket.side_effect = online_meanwhile

    await service.start('chan', open_=False)
    assert broadcaster.paused[-1] is False


async def test_pause_failure_does_not_leave_start_half_done(db, twitch):
    service, _, broadcaster = twitch
    broadcaster.fail_pause = True
    await service.start('chan', open_=True)
    assert service.active
    assert broadcaster.settle_asked, 'stale redemptions must still be settled'


async def test_resubscribe_settles_what_was_redeemed_while_deaf(db, twitch):
    """Redemptions made while the subscription was lost never arrive as events: the
    points must come back instead of hanging."""
    service, bot, broadcaster = twitch
    await service.start('chan', open_=True)
    bot.subscribe_websocket.reset_mock()
    broadcaster.settle_asked = False

    await service.resubscribe()

    bot.subscribe_websocket.assert_awaited_once()
    assert bot.subscribe_websocket.await_args.kwargs['token_for'] == 'chan'
    assert broadcaster.settle_asked
