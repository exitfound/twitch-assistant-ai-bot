"""The Twitch side of the channel-points rewards, against a fake broadcaster."""
import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import twitchio

from fakes import InProcessRoll

from src.core.utils import safe_format
from src.roll import game, specs
from src.roll.storage import save_action
from src.twitch.core.followers import FollowerCache
from src.twitch.roll.client import RollSlow, RollUnavailable
from src.twitch.roll.rewards import CANCELED, FULFILLED, RewardService, get_reward_ids


class FakeBroadcaster:
    def __init__(self) -> None:
        self.paused: list[bool] = []
        self.fail_pause = False
        self.settle_asked = False
        self.created: list[str] = []
        self.broken: dict[str, Exception] = {}     # reward id → what pausing it raises
        self.unfulfilled: dict[str, list] = {}     # reward id → its UNFULFILLED redemptions
        self.rewards: dict[str, SimpleNamespace] = {}  # reward id → the reward as Twitch holds it

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

    async def create_custom_reward(self, title, cost, *, prompt=None, max_per_user=None):
        await asyncio.sleep(0)      # a real request: another start() may run meanwhile
        self.created.append(title)
        # As twitchio does: a description makes the reward ask for text
        self.rewards[f'id-{title}'] = SimpleNamespace(
            id=f'id-{title}', title=title, cost=cost, prompt=prompt or '', input_required=prompt is not None,
            max_per_user_stream=SimpleNamespace(enabled=bool(max_per_user), value=max_per_user or 0), enabled=True,
        )
        return self.rewards[f'id-{title}']

    async def update_custom_reward(self, reward_id, **changes):
        if 'paused' in changes:
            if self.fail_pause:
                raise RuntimeError('twitch 500')
            if reward_id in self.broken:
                raise self.broken[reward_id]
            self.paused.append(changes['paused'])
            return SimpleNamespace(id=reward_id)
        reward = self.rewards[reward_id]
        for field, value in changes.items():
            setattr(reward, field, value)
        return reward


@pytest.fixture
def twitch():
    broadcaster = FakeBroadcaster()
    bot = SimpleNamespace(
        create_partialuser=lambda channel_id: broadcaster,
        subscribe_websocket=AsyncMock(),
        send_chat_message=AsyncMock(return_value=True),
        _http=SimpleNamespace(patch_custom_reward_redemption=AsyncMock()),
    )
    return RewardService(bot, InProcessRoll(bot, FollowerCache())), bot, broadcaster


async def test_start_creates_the_rewards_and_opens_them_live(db, twitch):
    service, _, broadcaster = twitch
    await service.start('chan', open_=True)
    assert service.active
    assert len(await get_reward_ids()) == len(specs.specs()) == 6
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


async def test_a_new_reward_without_a_nick_does_not_ask_for_text(db, twitch):
    """twitchio creates every reward with a description as one that asks for text: the
    rewards that take no nick are switched back at once, not on the next start."""
    service, _, broadcaster = twitch
    await service.start('chan', open_=True)
    asks = {action: broadcaster.rewards[reward_id].input_required for reward_id, action in service._actions.items()}
    assert asks == {spec.action: spec.input_required for spec in specs.specs()}
    assert asks[game.Action.FLASK] is False


async def test_two_starts_at_once_create_each_reward_once(db, twitch):
    """event_ready, a new login and the retry may all call start(): each would find the
    rewards missing and create them again."""
    service, _, broadcaster = twitch
    await asyncio.gather(service.start('chan', open_=True), service.start('chan', open_=True))
    assert len(broadcaster.created) == len(specs.specs())


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
    assert broadcaster.paused == [True] * (len(specs.specs()) - 2)
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

    async def decide(action, redemption_id, user, user_id, user_input):
        seen.update(action=action, user=user)
        return decision
    monkeypatch.setattr(service._roll, 'redeem', decide)
    reward_id, action = next(iter(service._actions.items()))
    payload = SimpleNamespace(id='r-1', reward=SimpleNamespace(id=reward_id),
                              user=SimpleNamespace(name='GoP', id='id-gop'), user_input='')
    await service.on_redemption(payload)

    assert seen == {'action': action, 'user': 'gop'}
    assert _statuses(bot) == ({'r-1': status} if status else {})


async def test_the_follow_question_goes_to_the_shared_cache_by_user_id(db, twitch, monkeypatch):
    """The chat's cache: a follow event clears it there, and the buyer is asked by id."""
    _, bot, _ = twitch
    followers = SimpleNamespace(is_follower=AsyncMock(return_value=False))
    service = RewardService(bot, InProcessRoll(bot, followers))
    await service.start('chan', open_=True)
    answers = []

    async def decide(bot_, action, redemption_id, user, user_input, *, follows):
        answers.append(await follows())
        return False
    monkeypatch.setattr('src.roll.redemption.handle_redemption', decide)
    reward_id = next(iter(service._actions))
    await service.on_redemption(SimpleNamespace(id='r-1', reward=SimpleNamespace(id=reward_id),
                                                user=SimpleNamespace(name='gop', id='42'), user_input=''))
    assert answers == [False]
    followers.is_follower.assert_awaited_once_with(bot, '42')


async def test_a_reward_of_another_app_is_not_touched(db, twitch, monkeypatch):
    service, bot, _ = twitch
    await service.start('chan', open_=True)
    decide = AsyncMock(return_value=True)
    monkeypatch.setattr(service._roll, 'redeem', decide)
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
    broadcaster.unfulfilled = {'id-' + specs.reward_title(game.Action.EXTRA)[:specs.TITLE_MAX]: [
        SimpleNamespace(id='applied', redeemed_at=before),
        SimpleNamespace(id='refused', redeemed_at=before),
        SimpleNamespace(id='unseen', redeemed_at=before),
        SimpleNamespace(id='live', redeemed_at=after),
    ]}
    await service.start('chan', open_=True)

    assert _statuses(bot) == {'applied': FULFILLED, 'refused': CANCELED, 'unseen': CANCELED}


def test_every_placeholder_the_notes_promise_reaches_the_descriptions(monkeypatch):
    """CONTENT.md lists what a reward description may use; one the code does not pass
    would send the raw template to Twitch."""
    promised = ('{ceiling} {step} {floor} {hold} {protect} {shield} {cleanse_protect} {flask} '
                '{luck_from} {luck_step} {luck_top} {choke_step} {agony_from} {agony_top} {agony_curse} '
                '{agony_throw_top} {bless_from} {throws} {pause} '
                '{window} {follower} {vip} {mod} {sub} {max}')
    monkeypatch.setattr(specs.Content, 'text', lambda key, **values: safe_format(promised, **values))
    for spec in specs.specs():
        assert '{' not in spec.prompt, spec.action


async def test_a_redemption_while_the_game_is_down_is_refunded_with_a_word(db, twitch, monkeypatch):
    """Without the game nothing is applied: the points go back and the buyer hears why."""
    service, bot, _ = twitch
    await service.start('chan', open_=True)
    monkeypatch.setattr(service._roll, 'redeem', AsyncMock(side_effect=RollUnavailable()))
    reward_id = next(iter(service._actions))
    await service.on_redemption(SimpleNamespace(id='r-1', reward=SimpleNamespace(id=reward_id),
                                                user=SimpleNamespace(name='gop', id='42'), user_input=''))
    assert _statuses(bot) == {'r-1': CANCELED}
    assert bot.send_chat_message.await_args.args[0] == 'texts.reward_unavailable'


async def test_without_the_game_the_rewards_do_not_start(db, twitch, monkeypatch):
    """No specs – nothing to create: the start fails, and the bot's retry brings it back later."""
    service, _, broadcaster = twitch
    monkeypatch.setattr(service._roll, 'specs', AsyncMock(side_effect=RollUnavailable()))
    with pytest.raises(RollUnavailable):
        await service.start('chan', open_=True)
    assert not service.active
    assert broadcaster.created == []


@pytest.mark.parametrize(('applied', 'status'), [(True, FULFILLED), (False, CANCELED)])
async def test_a_redemption_the_game_was_slow_on_is_settled_by_its_journal(db, twitch, monkeypatch, applied, status):
    """No answer in time: the game may have applied it. A minute later its journal decides,
    and the points do not hang until the next start."""
    from src.twitch.roll import rewards
    monkeypatch.setattr(rewards, 'RECHECK_SECONDS', 0)
    service, bot, _ = twitch
    await service.start('chan', open_=True)
    monkeypatch.setattr(service._roll, 'redeem', AsyncMock(side_effect=RollSlow()))
    answers = [RollUnavailable(), {'r-1'} if applied else set()]
    monkeypatch.setattr(service._roll, 'applied', AsyncMock(side_effect=answers))
    reward_id = next(iter(service._actions))
    await service.on_redemption(SimpleNamespace(id='r-1', reward=SimpleNamespace(id=reward_id),
                                                user=SimpleNamespace(name='gop', id='42'), user_input=''))
    assert _statuses(bot) == {}
    await asyncio.gather(*service._rechecks)
    assert _statuses(bot) == {'r-1': status}
    bot.send_chat_message.assert_not_awaited()


async def test_a_redemption_still_in_the_games_hands_is_not_settled_as_stale(db, twitch, monkeypatch):
    """A resubscribe while the game applies a redemption (or a recheck waits for it) must
    not refund it: the game may be about to apply it."""
    service, bot, broadcaster = twitch
    await service.start('chan', open_=True)
    reward_id = next(iter(service._actions))
    before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    broadcaster.unfulfilled = {reward_id: [SimpleNamespace(id='busy', redeemed_at=before),
                                           SimpleNamespace(id='lost', redeemed_at=before)]}
    service._busy.add('busy')
    await service.resubscribe()
    assert _statuses(bot) == {'lost': CANCELED}


async def test_a_redemption_whose_connection_dropped_is_checked_not_refunded(db, twitch, monkeypatch):
    """Sent, then the connection dropped: the game may have applied it, so no refund at once."""
    service, bot, _ = twitch
    await service.start('chan', open_=True)
    monkeypatch.setattr(service._roll, 'redeem', AsyncMock(side_effect=RollSlow('dropped')))
    reward_id = next(iter(service._actions))
    await service.on_redemption(SimpleNamespace(id='r-1', reward=SimpleNamespace(id=reward_id),
                                                user=SimpleNamespace(name='gop', id='42'), user_input=''))
    assert _statuses(bot) == {}
    assert 'r-1' in service._busy
    bot.send_chat_message.assert_not_awaited()
    for task in list(service._rechecks):
        task.cancel()
