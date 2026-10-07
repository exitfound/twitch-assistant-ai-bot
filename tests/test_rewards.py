"""The Twitch side of the channel-points rewards, against a fake broadcaster."""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import twitchio

from src.twitch.local.roll import game, rewards
from src.twitch.local.roll.rewards import CANCELED, FULFILLED, RewardService
from src.twitch.local.roll.storage import get_reward_ids, save_action


class FakeBroadcaster:
    def __init__(self) -> None:
        self.paused: list[bool] = []
        self.fail_pause = False
        self.settle_asked = False
        self.created: list[str] = []
        self.broken: dict[str, Exception] = {}     # reward id → what pausing it raises
        self.unfulfilled: dict[str, list] = {}     # reward id → its UNFULFILLED redemptions

    async def fetch_custom_rewards(self, *, ids=None, manageable=True):
        if ids is None:
            return []
        self.settle_asked = True
        return [SimpleNamespace(id=reward_id, fetch_redemptions=self._redemptions(reward_id))
                for reward_id in ids if reward_id in self.unfulfilled]

    def _redemptions(self, reward_id):
        def fetch(*, status):
            async def pages():
                for redemption in self.unfulfilled[reward_id]:
                    yield redemption
            return pages()
        return fetch

    async def create_custom_reward(self, title, cost, **kwargs):
        await asyncio.sleep(0)      # a real request: another start() may run meanwhile
        self.created.append(title)
        return SimpleNamespace(id=f'id-{title}')

    async def update_custom_reward(self, reward_id, **changes):
        if 'paused' in changes:
            if self.fail_pause:
                raise RuntimeError('twitch 500')
            if reward_id in self.broken:
                raise self.broken[reward_id]
            self.paused.append(changes['paused'])
        return SimpleNamespace(id=reward_id)


@pytest.fixture
def twitch():
    broadcaster = FakeBroadcaster()
    bot = SimpleNamespace(
        create_partialuser=lambda channel_id: broadcaster,
        subscribe_websocket=AsyncMock(),
        _http=SimpleNamespace(patch_custom_reward_redemption=AsyncMock()),
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


async def test_two_starts_at_once_create_each_reward_once(db, twitch):
    """event_ready, a new login and the retry may all call start(): each would find the
    rewards missing and create them again."""
    service, _, broadcaster = twitch
    await asyncio.gather(service.start('chan', open_=True), service.start('chan', open_=True))
    assert len(broadcaster.created) == 5


def _not_found() -> twitchio.HTTPException:
    return twitchio.HTTPException('x', status=404, extra={'message': 'Not Found'})


async def test_one_broken_reward_does_not_stop_the_pause_of_the_rest(db, twitch):
    """The first reward deleted by hand left every one after it open while offline."""
    service, _, broadcaster = twitch
    await service.start('chan', open_=True)
    first, second = list(service._actions)[:2]
    broadcaster.broken = {first: _not_found(),
                          second: twitchio.HTTPException('x', status=500, extra={'message': 'down'})}
    broadcaster.paused.clear()

    assert not await service._set_paused(True)
    assert broadcaster.paused == [True] * 3
    # The deleted one is forgotten until the next start; the one that failed is kept
    assert first not in service._actions
    assert second in service._actions


async def test_a_deleted_reward_alone_is_not_a_failure(db, twitch):
    service, _, broadcaster = twitch
    await service.start('chan', open_=True)
    broadcaster.broken = {next(iter(service._actions)): _not_found()}
    assert await service._set_paused(True)


def _statuses(bot) -> dict[str, str]:
    """Redemption id → the status the bot set on Twitch."""
    return {c.kwargs['id']: c.kwargs['status']
            for c in bot._http.patch_custom_reward_redemption.await_args_list}


@pytest.mark.parametrize(('decision', 'status'), [(True, FULFILLED), (False, CANCELED), (None, None)])
async def test_a_redemption_gets_the_status_of_its_outcome(db, twitch, monkeypatch, decision, status):
    """FULFILLED keeps the points, CANCELED gives them back; a repeat is left alone."""
    service, bot, _ = twitch
    await service.start('chan', open_=True)
    seen = {}

    async def decide(bot_, action, redemption_id, user, user_input):
        seen.update(action=action, user=user)
        return decision
    monkeypatch.setattr(rewards, 'handle_redemption', decide)
    reward_id, action = next(iter(service._actions.items()))
    payload = SimpleNamespace(id='r-1', reward=SimpleNamespace(id=reward_id),
                              user=SimpleNamespace(name='GoP'), user_input='')
    await service.on_redemption(payload)

    assert seen == {'action': action, 'user': 'gop'}
    assert _statuses(bot) == ({'r-1': status} if status else {})


async def test_a_reward_of_another_app_is_not_touched(db, twitch, monkeypatch):
    service, bot, _ = twitch
    await service.start('chan', open_=True)
    decide = AsyncMock(return_value=True)
    monkeypatch.setattr(rewards, 'handle_redemption', decide)
    await service.on_redemption(SimpleNamespace(id='r-1', reward=SimpleNamespace(id='someone-else'),
                                                user=SimpleNamespace(name='gop'), user_input=''))
    decide.assert_not_awaited()
    assert _statuses(bot) == {}


async def test_stale_redemptions_are_settled_by_the_journal(db, twitch):
    """After a crash or a lost subscription: what the game applied is fulfilled, what it
    never saw is refunded, and what came after the start arrives as an event."""
    service, bot, broadcaster = twitch
    now = datetime.datetime.now(datetime.UTC)
    before, after = now - datetime.timedelta(minutes=5), now + datetime.timedelta(minutes=5)
    await save_action('applied', 's', game.Action.EXTRA, 'gop', '', 'gop', None, 50, game.Status.OK)
    await save_action('refused', 's', game.Action.CURSE, 'gop', 'x', None, None, None, game.Status.UNKNOWN_TARGET)
    broadcaster.unfulfilled = {'id-' + rewards.reward_title(game.Action.EXTRA)[:rewards.TITLE_MAX]: [
        SimpleNamespace(id='applied', redeemed_at=before),
        SimpleNamespace(id='refused', redeemed_at=before),
        SimpleNamespace(id='unseen', redeemed_at=before),
        SimpleNamespace(id='live', redeemed_at=after),
    ]}
    await service.start('chan', open_=True)

    assert _statuses(bot) == {'applied': FULFILLED, 'refused': CANCELED, 'unseen': CANCELED}
