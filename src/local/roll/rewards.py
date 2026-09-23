"""Channel-points rewards: creation on Twitch, subscription, fulfilling and refunding points.

Only Twitch is here. What a reward does to the game – src/local/roll/redemption.py and
src/local/roll/game.py.

Why the bot creates the rewards itself. Twitch lets only the app that created a reward
fulfill a redemption or refund its points. A reward made by hand in the streamer's
dashboard is out of the bot's reach – it could not refund points for a typo in a nick.

A redemption is handled at once: the bot applies the reward and immediately fulfills
it or cancels it with a refund. Neither the streamer nor the viewer has to click
anything. While the bot is off, the rewards are paused.
"""
import dataclasses
import datetime
import logging

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from src.core.config import Rewards, Roll
from src.core.content import Content
from src.local.roll import game
from src.local.roll.redemption import handle_redemption
from src.local.roll.storage import get_action_status, get_reward_ids, save_reward_id
from src.local.roll.texts import curse_values, reward_title

logger = logging.getLogger(__name__)

# The scope without which a reward cannot be created and points cannot be refunded.
# Only the broadcaster can grant it – a moderator bot cannot have it
REWARDS_SCOPE = 'channel:manage:redemptions'

# Twitch limits on reward fields
TITLE_MAX = 45
PROMPT_MAX = 200

FULFILLED = 'FULFILLED'
CANCELED = 'CANCELED'


@dataclasses.dataclass(frozen=True)
class RewardSpec:
    action: str
    cost: int
    input_required: bool
    max_per_user: int           # per stream, 0 – no limit

    @property
    def title(self) -> str:
        return reward_title(self.action)[:TITLE_MAX]

    @property
    def prompt(self) -> str:
        # The reward's description on Twitch; on a reward that takes a nick it is also
        # the caption of the input field
        return Content.text(
            f'reward_{self.action}_prompt', ceiling=Rewards.CURSE_CEILING,
            protect=Rewards.REROLL_PROTECT_MINUTES, cleanse_protect=Rewards.CLEANSE_PROTECT_MINUTES,
            shield=Rewards.SHIELD_MINUTES, series=Rewards.EXTRA_SERIES, pause=Rewards.EXTRA_PAUSE_MINUTES,
            max=Roll.MAX, **curse_values(),
        )[:PROMPT_MAX]


def _specs() -> list[RewardSpec]:
    return [
        RewardSpec(game.Action.EXTRA, Rewards.COST_EXTRA, False, Rewards.EXTRA_MAX_PER_USER),
        RewardSpec(game.Action.REROLL, Rewards.COST_REROLL, True, Rewards.REROLL_MAX_PER_USER),
        RewardSpec(game.Action.CURSE, Rewards.COST_CURSE, True, Rewards.CURSE_MAX_PER_USER),
        RewardSpec(game.Action.SHIELD, Rewards.COST_SHIELD, False, Rewards.SHIELD_MAX_PER_USER),
        RewardSpec(game.Action.CLEANSE, Rewards.COST_CLEANSE, True, Rewards.CLEANSE_MAX_PER_USER),
    ]


def _changes(reward: twitchio.CustomReward, spec: RewardSpec) -> dict:
    """Fields that differ from CONTENT.md and .env. Empty dict – no request needed."""
    changes: dict = {}
    if reward.title != spec.title:
        changes['title'] = spec.title
    if reward.cost != spec.cost:
        changes['cost'] = spec.cost
    if reward.input_required != spec.input_required:
        changes['input_required'] = spec.input_required
    if reward.prompt != spec.prompt:
        changes['prompt'] = spec.prompt
    limit = reward.max_per_user_stream
    if (limit.value if limit.enabled else 0) != spec.max_per_user:
        changes['max_per_user'] = spec.max_per_user
    if not reward.enabled:
        changes['enabled'] = True
    return changes


class RewardService:

    def __init__(self, bot) -> None:
        self._bot = bot
        self._channel_id: str | None = None
        self._actions: dict[str, str] = {}      # Twitch reward id → action
        # Whether the rewards should be open, i.e. the stream is live. Written by set_open()
        # even before start() finishes, so a stream starting mid-start is not lost
        self._open = False
        self.active = False

    async def start(self, channel_id: str, *, open_: bool) -> None:
        """Create or update the rewards, subscribe, settle stale redemptions.

        open_ – whether the stream is live. Offline the game is closed and rewards stay paused.
        """
        if self.active:
            return
        self._channel_id = channel_id
        self._open = open_
        # Anything redeemed before the subscription will never arrive as an event
        started = datetime.datetime.now(datetime.UTC)
        await self._sync()
        await self._subscribe()
        self.active = True
        # The subscription is live from here: a failure below is logged, not raised, or
        # the service would stay half-started with no retry on reconnect
        try:
            await self._set_paused(not self._open)
            logger.info(
                'Награды за баллы канала подключены (%s): %s',
                'эфир идёт' if self._open else 'на паузе до начала эфира',
                ', '.join(s.title for s in _specs()),
            )
        except Exception:
            logger.exception('Награды подключены, но паузу выставить не удалось')
        try:
            await self._settle_stale(started)
        except Exception:
            logger.exception('Не удалось разобрать зависшие выкупы')

    async def resubscribe(self) -> None:
        """Subscribe again after the socket watch found the subscription lost.

        What was redeemed while the bot was deaf never arrives as an event: those points
        are refunded, the same way as after a crash.
        """
        started = datetime.datetime.now(datetime.UTC)
        await self._subscribe()
        try:
            await self._settle_stale(started)
        except Exception:
            logger.exception('Не удалось разобрать зависшие выкупы')

    async def _subscribe(self) -> None:
        await self._bot.subscribe_websocket(
            eventsub.ChannelPointsRedeemAddSubscription(broadcaster_user_id=self._channel_id),
            as_bot=False, token_for=self._channel_id,
        )

    async def stop(self) -> None:
        """Pause the rewards: while the bot is away, there is nothing to spend points on."""
        if not self.active:
            return
        self.active = False
        try:
            await self._set_paused(True)
            logger.info('Награды за баллы канала поставлены на паузу')
        except Exception:
            logger.exception('Не удалось поставить награды на паузу')

    async def set_open(self, open_: bool) -> None:
        """The stream started or ended: unpause the rewards or pause them.

        Remembered even while the service is not active: start() applies the latest state.
        """
        self._open = open_
        if not self.active:
            return
        try:
            await self._set_paused(not open_)
            logger.info('Награды за баллы канала %s', 'сняты с паузы' if open_ else 'на паузе до начала эфира')
        except Exception:
            logger.exception('Не удалось переключить паузу наград')

    async def on_redemption(self, payload: twitchio.ChannelPointsRedemptionAdd) -> None:
        action = self._actions.get(payload.reward.id)
        if action is None:
            return                  # a reward of another app, not the bot's
        decision = await handle_redemption(
            self._bot, action, payload.id, (payload.user.name or '').lower(), payload.user_input,
        )
        if decision is None:
            return
        await self._set_status(payload.reward.id, payload.id, FULFILLED if decision else CANCELED)

    def _broadcaster(self) -> twitchio.PartialUser:
        return self._bot.create_partialuser(self._channel_id)

    async def _sync(self) -> None:
        """Bring the rewards on Twitch in line with CONTENT.md and .env.

        A reward is looked up by its stored id, and failing that by title among this
        app's rewards: that survives both a lost rewards table and a rename in
        CONTENT.md.
        """
        broadcaster = self._broadcaster()
        existing = {r.id: r for r in await broadcaster.fetch_custom_rewards(manageable=True)}
        by_title = {r.title: r for r in existing.values()}
        stored = await get_reward_ids()
        actions: dict[str, str] = {}
        for spec in _specs():
            reward = existing.get(stored.get(spec.action, '')) or by_title.get(spec.title)
            if reward is None:
                reward = await broadcaster.create_custom_reward(
                    spec.title, spec.cost, prompt=spec.prompt or None,
                    max_per_user=spec.max_per_user or None,
                )
                logger.info('Создана награда «%s» за %d баллов', spec.title, spec.cost)
            else:
                changes = _changes(reward, spec)
                if changes:
                    reward = await broadcaster.update_custom_reward(reward.id, **changes)
                    logger.info('Награда «%s» обновлена: %s', spec.title, ', '.join(changes))
            await save_reward_id(spec.action, reward.id)
            actions[reward.id] = spec.action
        self._actions = actions

    async def _set_paused(self, paused: bool) -> None:
        broadcaster = self._broadcaster()
        for reward_id in self._actions:
            await broadcaster.update_custom_reward(reward_id, paused=paused)

    async def _set_status(self, reward_id: str, redemption_id: str, status: str) -> None:
        # CustomRewardRedemption.fulfill() in twitchio 3.x sends the channel id
        # instead of the redemption id, so the status is set with a direct request –
        # the same way for events and for stale redemptions
        try:
            await self._bot._http.patch_custom_reward_redemption(
                broadcaster_id=self._channel_id, token_for=self._channel_id,
                reward_id=reward_id, id=redemption_id, status=status,
            )
        except Exception:
            logger.exception('Не удалось выставить статус %s выкупу %s', status, redemption_id)

    async def _settle_stale(self, started: datetime.datetime) -> None:
        """Settle redemptions left without a status before the subscription.

        That happens when the bot crashed before it could pause the rewards, or while the
        subscription was lost. They are not applied retroactively – the rolls have moved on
        since – so the points are refunded. The exception is a reward that was applied
        but not yet fulfilled: that one gets fulfilled.
        """
        broadcaster = self._broadcaster()
        rewards = await broadcaster.fetch_custom_rewards(ids=list(self._actions), manageable=True)
        settled = 0
        for reward in rewards:
            # Collect first: changing the status drops the redemption from the result
            # and throws off the pagination
            pending = [r async for r in reward.fetch_redemptions(status='UNFULFILLED')]
            for redemption in pending:
                if redemption.redeemed_at >= started:
                    continue        # redeemed after the subscription, will arrive as an event
                applied = await get_action_status(redemption.id) == game.Status.OK
                await self._set_status(reward.id, redemption.id, FULFILLED if applied else CANCELED)
                settled += 1
        if settled:
            logger.warning('Разобрано зависших выкупов: %d', settled)


class RewardComponent(commands.Component):

    def __init__(self, service: RewardService) -> None:
        self._service = service

    @commands.Component.listener()
    async def event_custom_redemption_add(self, payload: twitchio.ChannelPointsRedemptionAdd) -> None:
        try:
            await self._service.on_redemption(payload)
        except Exception:
            logger.exception('Обработка награды за баллы не удалась')
