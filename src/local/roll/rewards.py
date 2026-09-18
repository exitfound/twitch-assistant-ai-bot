"""Награды за баллы канала: создание в Twitch, подписка, подтверждение и возврат баллов.

Здесь только Twitch. Что награда делает с игрой – src/local/roll/redemption.py и
src/local/roll/game.py.

Почему награды создаёт сам бот. Twitch разрешает подтвердить выкуп или
вернуть за него баллы только приложению, которое создало награду. Заведённая
руками в панели стримера награда для бота недоступна – вернуть баллы за
опечатку в нике он бы не смог.

Выкуп обрабатывается сразу: бот применяет награду и тут же подтверждает её
или отменяет с возвратом баллов. Ни стримеру, ни зрителю нажимать ничего не
нужно. Пока бот выключен, награды стоят на паузе.
"""
import dataclasses
import datetime
import logging

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from src.core.config import Rewards
from src.core.content import Content
from src.local.roll import game
from src.local.roll.redemption import handle_redemption
from src.local.roll.storage import get_action_status, get_reward_ids, save_reward_id
from src.local.roll.texts import curse_values, reward_title

logger = logging.getLogger(__name__)

# Право, без которого награду не создать и баллы не вернуть. Выдаёт его только
# владелец канала – у бота-модератора его быть не может
REWARDS_SCOPE = 'channel:manage:redemptions'

# Ограничения Twitch на поля награды
TITLE_MAX = 45
PROMPT_MAX = 200

FULFILLED = 'FULFILLED'
CANCELED = 'CANCELED'


@dataclasses.dataclass(frozen=True)
class RewardSpec:
    action: str
    cost: int
    input_required: bool
    max_per_user: int           # за эфир, 0 – без лимита

    @property
    def title(self) -> str:
        return reward_title(self.action)[:TITLE_MAX]

    @property
    def prompt(self) -> str:
        # В Twitch описание награды включает поле ввода, поэтому оно есть
        # только у наград, где нужен ник
        if not self.input_required:
            return ''
        return Content.text(
            f'reward_{self.action}_prompt', ceiling=Rewards.CURSE_CEILING,
            protect=Rewards.REROLL_PROTECT_MINUTES, **curse_values(),
        )[:PROMPT_MAX]


def _specs() -> list[RewardSpec]:
    return [
        RewardSpec(game.ACTION_EXTRA, Rewards.COST_EXTRA, False, 0),
        RewardSpec(game.ACTION_REROLL, Rewards.COST_REROLL, True, Rewards.ATTACK_MAX_PER_USER),
        RewardSpec(game.ACTION_CURSE, Rewards.COST_CURSE, True, Rewards.ATTACK_MAX_PER_USER),
        RewardSpec(game.ACTION_SHIELD, Rewards.COST_SHIELD, False, 0),
    ]


def _changes(reward: twitchio.CustomReward, spec: RewardSpec) -> dict:
    """Поля, которые разошлись с CONTENT.md и .env. Пустой dict – запрос не нужен."""
    changes: dict = {}
    if reward.title != spec.title:
        changes['title'] = spec.title
    if reward.cost != spec.cost:
        changes['cost'] = spec.cost
    if reward.input_required != spec.input_required:
        changes['input_required'] = spec.input_required
    if spec.input_required and reward.prompt != spec.prompt:
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
        self._actions: dict[str, str] = {}      # id награды в Twitch → действие
        self.active = False

    async def start(self, channel_id: str, *, open_: bool) -> None:
        """Создать или обновить награды, подписаться, разобрать зависшие.

        open_ – идёт ли эфир. Без эфира игра закрыта, и награды остаются на паузе.
        """
        if self.active:
            return
        self._channel_id = channel_id
        # Всё, что выкупили до подписки, событием уже не придёт
        started = datetime.datetime.now(datetime.timezone.utc)
        await self._sync()
        await self._bot.subscribe_websocket(
            eventsub.ChannelPointsRedeemAddSubscription(broadcaster_user_id=channel_id),
            as_bot=False, token_for=channel_id,
        )
        self.active = True
        await self._set_paused(not open_)
        logger.info(
            'Награды за баллы канала подключены (%s): %s',
            'эфир идёт' if open_ else 'на паузе до начала эфира', ', '.join(s.title for s in _specs()),
        )
        await self._settle_stale(started)

    async def stop(self) -> None:
        """Поставить награды на паузу: пока бота нет, баллы списывать не за что."""
        if not self.active:
            return
        self.active = False
        try:
            await self._set_paused(True)
            logger.info('Награды за баллы канала поставлены на паузу')
        except Exception:
            logger.exception('Не удалось поставить награды на паузу')

    async def set_open(self, open_: bool) -> None:
        """Эфир начался или закончился: снять награды с паузы или поставить на неё."""
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
            return                  # другая награда стримера, не наша
        decision = await handle_redemption(
            self._bot, action, payload.id, (payload.user.name or '').lower(), payload.user_input,
        )
        if decision is None:
            return
        await self._set_status(payload.reward.id, payload.id, FULFILLED if decision else CANCELED)

    def _broadcaster(self) -> twitchio.PartialUser:
        return self._bot.create_partialuser(self._channel_id)

    async def _sync(self) -> None:
        """Привести награды в Twitch к CONTENT.md и .env.

        Награда ищется по сохранённому id, а если его нет – по названию среди
        наград этого приложения: так переживается и потеря таблицы rewards,
        и переименование в CONTENT.md.
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
        # CustomRewardRedemption.fulfill() в twitchio 3.x отправляет id канала
        # вместо id выкупа, поэтому статус ставим запросом напрямую – одинаково
        # для событий и для зависших выкупов
        try:
            await self._bot._http.patch_custom_reward_redemption(
                broadcaster_id=self._channel_id, token_for=self._channel_id,
                reward_id=reward_id, id=redemption_id, status=status,
            )
        except Exception:
            logger.exception('Не удалось выставить статус %s выкупу %s', status, redemption_id)

    async def _settle_stale(self, started: datetime.datetime) -> None:
        """Разобрать выкупы, оставшиеся без статуса с прошлого запуска.

        Так бывает, если бот упал и не успел поставить награды на паузу.
        Применять их задним числом нельзя – сессия и роллы уже другие, –
        поэтому баллы возвращаются. Исключение – награда, которую применили,
        но не успели подтвердить: её подтверждаем.
        """
        broadcaster = self._broadcaster()
        rewards = await broadcaster.fetch_custom_rewards(ids=list(self._actions), manageable=True)
        settled = 0
        for reward in rewards:
            # Сначала собираем: смена статуса выкидывает выкуп из выборки и
            # сбивает постраничный обход
            pending = [r async for r in reward.fetch_redemptions(status='UNFULFILLED')]
            for redemption in pending:
                if redemption.redeemed_at >= started:
                    continue        # выкуплен уже после подписки, придёт событием
                applied = await get_action_status(redemption.id) == game.OK
                await self._set_status(reward.id, redemption.id, FULFILLED if applied else CANCELED)
                settled += 1
        if settled:
            logger.warning('Разобрано зависших выкупов с прошлого запуска: %d', settled)


class RewardComponent(commands.Component):

    def __init__(self, service: RewardService) -> None:
        self._service = service

    @commands.Component.listener()
    async def event_custom_redemption_add(self, payload: twitchio.ChannelPointsRedemptionAdd) -> None:
        try:
            await self._service.on_redemption(payload)
        except Exception:
            logger.exception('Обработка награды за баллы не удалась')
