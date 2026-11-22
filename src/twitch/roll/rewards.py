"""Channel-points rewards: creation on Twitch, subscription, fulfilling and refunding points.

Only Twitch is here. Which rewards there are and what one does to the game is the game
service's: the specs come from it (src/roll/specs.py), and so does the decision on each
redemption (src/roll/redemption.py).

Why the bot creates the rewards itself. Twitch lets only the app that created a reward
fulfill a redemption or refund its points. A reward made by hand in the streamer's
dashboard is out of the bot's reach – it could not refund points for a typo in a nick.

A redemption is handled at once: the bot applies the reward and immediately fulfills
it or cancels it with a refund. Neither the streamer nor the viewer has to click
anything. While the bot is off, the rewards are paused.
"""
import asyncio
import datetime
import logging

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from src.core.content import Content
from src.core.database import get_db, transaction
from src.twitch.roll.client import RollLink, RollSlow, RollUnavailable, Spec

logger = logging.getLogger(__name__)

# The scope without which a reward cannot be created and points cannot be refunded.
# Only the broadcaster can grant it – a moderator bot cannot have it
REWARDS_SCOPE = 'channel:manage:redemptions'

FULFILLED = 'FULFILLED'
CANCELED = 'CANCELED'

# A redemption the game did not answer in time is looked up in its journal this much later,
# and again while the game cannot say; after that it waits for the next start
RECHECK_SECONDS = 60
RECHECK_TRIES = 10


async def get_reward_ids() -> dict[str, str]:
    """{action: Twitch reward id} – which rewards the bot has already created."""
    db = await get_db()
    async with db.execute('SELECT action, reward_id FROM rewards') as cursor:
        rows = await cursor.fetchall()
    return dict(rows)


async def save_reward_id(action: str, reward_id: str) -> None:
    async with transaction() as db:
        await db.execute(
            'INSERT INTO rewards (action, reward_id) VALUES (?, ?)'
            ' ON CONFLICT(action) DO UPDATE SET reward_id = excluded.reward_id',
            (action, reward_id),
        )


def _changes(reward: twitchio.CustomReward, spec: Spec) -> dict:
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

    def __init__(self, bot, roll: RollLink) -> None:
        self._bot = bot
        self._roll = roll
        self._channel_id: str | None = None
        self._actions: dict[str, str] = {}      # Twitch reward id → action
        self._titles: dict[str, str] = {}       # action → its title, for the line when the game is down
        self._rechecks: set[asyncio.Task] = set()
        # Redemptions the game is applying or that wait for a recheck: not stale, whatever
        # Twitch says, or settling them would refund one the game is about to apply
        self._busy: set[str] = set()
        # Whether the rewards should be open, i.e. the stream is live. Written by set_open()
        # even before start() finishes, so a stream starting mid-start is not lost
        self._open = False
        self.active = False
        # start() comes from event_ready, a new login and the retry after a failure:
        # two at once would both find a reward missing and create it twice
        self._starting = asyncio.Lock()

    async def start(self, channel_id: str, *, open_: bool) -> None:
        """Create or update the rewards, subscribe, settle stale redemptions.

        open_ – whether the stream is live. Offline the game is closed and rewards stay paused.
        """
        async with self._starting:
            if self.active:
                return
            self._channel_id = channel_id
            self._open = open_
            # Anything redeemed before the subscription will never arrive as an event
            started = datetime.datetime.now(datetime.UTC)
            # Without the game's specs there is nothing to create: the start fails and is retried
            specs = await self._roll.specs()
            await self._sync(specs)
            await self._subscribe()
            self.active = True
        # The subscription is live from here: a failure below is logged, not raised, or
        # the service would stay half-started with no retry on reconnect
        if await self._set_paused(not self._open):
            logger.info(
                'Награды за баллы канала подключены (%s): %s',
                'эфир идёт' if self._open else 'на паузе до начала эфира',
                ', '.join(spec.title for spec in specs),
            )
        else:
            logger.warning('Награды подключены, но паузу выставить удалось не всем')
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
        for task in list(self._rechecks):
            task.cancel()
        if not self.active:
            return
        self.active = False
        if await self._set_paused(True):
            logger.info('Награды за баллы канала поставлены на паузу')

    def drop(self) -> None:
        """The channel's token is gone: stop without touching Twitch, which would refuse.

        start() brings the service back once a new token arrives.
        """
        self.active = False

    async def set_open(self, open_: bool) -> None:
        """The stream started or ended: unpause the rewards or pause them.

        Remembered even while the service is not active: start() applies the latest state.
        """
        self._open = open_
        if not self.active:
            return
        if await self._set_paused(not open_):
            logger.info('Награды за баллы канала %s', 'сняты с паузы' if open_ else 'на паузе до начала эфира')

    async def on_redemption(self, payload: twitchio.ChannelPointsRedemptionAdd) -> None:
        action = self._actions.get(payload.reward.id)
        if action is None:
            return                  # a reward of another app, not the bot's
        user = (payload.user.name or '').lower()
        self._busy.add(payload.id)
        try:
            decision = await self._roll.redeem(action, payload.id, user, payload.user.id, payload.user_input)
        except RollSlow:
            # Maybe applied, maybe not: the game's journal tells, a minute later. Still busy
            logger.warning('Игра не ответила на выкуп %s, но могла применить – сверю по журналу', payload.id)
            task = asyncio.create_task(self._recheck(payload.reward.id, payload.id))
            self._rechecks.add(task)
            task.add_done_callback(self._rechecks.discard)
            return
        except RollUnavailable:
            self._busy.discard(payload.id)
            # The game is down: the points go back, and the buyer hears why
            logger.warning('Игра недоступна – выкуп %s от %s отменён', action, user)
            decision = False
            await self._bot.send_chat_message(Content.text(
                'reward_unavailable', user=user, reward=self._titles.get(action, action),
            ))
        else:
            self._busy.discard(payload.id)
        if decision is None:
            return
        await self._set_status(payload.reward.id, payload.id, FULFILLED if decision else CANCELED)

    async def _recheck(self, reward_id: str, redemption_id: str) -> None:
        """Settle a redemption the game was too slow to answer by its journal: applied –
        fulfilled, anything else – refunded, as a stale one is after a start."""
        try:
            for _ in range(RECHECK_TRIES):
                await asyncio.sleep(RECHECK_SECONDS)
                try:
                    applied = redemption_id in await self._roll.applied([redemption_id])
                except RollUnavailable:
                    continue
                await self._set_status(reward_id, redemption_id, FULFILLED if applied else CANCELED)
                logger.info('Выкуп %s сверен по журналу игры: %s', redemption_id,
                            'применён' if applied else 'возврат')
                return
            logger.warning('Выкуп %s так и не сверен – его разберёт следующий старт наград', redemption_id)
        finally:
            self._busy.discard(redemption_id)

    def _broadcaster(self) -> twitchio.PartialUser:
        return self._bot.create_partialuser(self._channel_id)

    async def _sync(self, specs: list[Spec]) -> None:
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
        for spec in specs:
            reward = existing.get(stored.get(spec.action, '')) or by_title.get(spec.title)
            if reward is None:
                reward = await broadcaster.create_custom_reward(
                    spec.title, spec.cost, prompt=spec.prompt or None,
                    max_per_user=spec.max_per_user or None,
                )
                logger.info('Создана награда «%s» за %d баллов', spec.title, spec.cost)
            # A new reward is checked too: twitchio creates any reward with a description as
            # one that asks for text, and a reward that takes no nick must not ask for it
            changes = _changes(reward, spec)
            if changes:
                reward = await broadcaster.update_custom_reward(reward.id, **changes)
                logger.info('Награда «%s» обновлена: %s', spec.title, ', '.join(changes))
            await save_reward_id(spec.action, reward.id)
            actions[reward.id] = spec.action
        self._actions = actions
        self._titles = {spec.action: spec.title for spec in specs}

    async def _set_paused(self, paused: bool) -> bool:
        """Pause or unpause every reward; True – all of them. Never raises.

        One failing reward does not stop the rest: otherwise a reward deleted by hand
        would leave the ones after it open offline or paused on stream. A deleted one
        (404) is forgotten until the next start, which creates it again.
        """
        broadcaster = self._broadcaster()
        done = True
        for reward_id, action in list(self._actions.items()):
            try:
                await broadcaster.update_custom_reward(reward_id, paused=paused)
            except twitchio.HTTPException as e:
                if e.status == 404:
                    logger.warning('Награда %s удалена на Twitch – без неё до перезапуска', action)
                    self._actions.pop(reward_id, None)
                    continue
                logger.warning('Награда %s: пауза не переключилась: %s', action, e)
                done = False
            except Exception as e:
                logger.warning('Награда %s: пауза не переключилась: %r', action, e)
                done = False
        return done

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
        stale = []
        for reward in rewards:
            # Collect first: changing the status drops the redemption from the result
            # and throws off the pagination
            pending = [r async for r in reward.fetch_redemptions(status='UNFULFILLED')]
            # Redeemed after the subscription: it will arrive as an event. In the game's hands
            # right now: not stale at all
            stale += [(reward.id, r.id) for r in pending if r.redeemed_at < started and r.id not in self._busy]
        if not stale:
            return
        # The game's journal tells an applied redemption from one it never saw
        applied = await self._roll.applied([redemption_id for _, redemption_id in stale])
        settled = 0
        for reward_id, redemption_id in stale:
            await self._set_status(reward_id, redemption_id, FULFILLED if redemption_id in applied else CANCELED)
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
