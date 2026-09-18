"""Компонент чата: приём сообщений, диспетчер команд, follow-события.

Единственное место в core, которое знает про фичи: здесь команды из
src/gemini, src/local и src/local/roll попадают в реестр.
"""
import logging
import math
import re

import twitchio
from twitchio.ext import commands

from src.core.commands import (
    KIND_GEMINI, ROLE_SUB_VIP_MOD_BROADCASTER, ROLE_VIP_MOD_BROADCASTER,
    CommandContext, CommandRegistry,
)
from src.core.config import Cooldown, Follow, Picture, Quota
from src.core.content import Content
from src.core.database import (
    count_bot_uses, oldest_bot_use_age, record_bot_use, save_chat_message,
)
from src.core.followers import FollowerCache
from src.core.utils import SOSUR_RE, SOSUR_VARIANTS  # noqa: F401  (SOSUR_VARIANTS – публичная точка правки списка)
from src.gemini.commands import (
    handle_ask, handle_default, handle_summary, handle_versus, handle_who,
)
from src.gemini.picture.command import handle_ascii
from src.local.commands import handle_defact, handle_fact, handle_help, handle_stats
from src.local.follow import handle_follow
from src.local.roll.command import handle_roll
from src.local.roll.perks import on_chat as roll_perks_on_chat

logger = logging.getLogger(__name__)

FACT_TRIGGER = '!fact'
DEFACT_TRIGGER = '!defact'
STATS_TRIGGER = '!stat'
HELP_TRIGGER = '!help-bot'
ASK_TRIGGER = '!ask'
SUMMARY_TRIGGER = '!summary'
WHO_TRIGGER = '!who'
VERSUS_TRIGGER = '!versus'
ROLL_TRIGGER = '!roll'
ASCII_TRIGGER = '!ascii'

# Статус зрителя – от него зависит длительность кулдауна
STATUS_BROADCASTER = 'broadcaster'
STATUS_MODERATOR = 'moderator'
STATUS_SUB = 'subscriber'
STATUS_VIP = 'vip'
STATUS_REGULAR = 'regular'
# Область кулдауна для подсказки «зафоловься»: чтобы бот не повторял её
# на каждое сообщение незафоловленного зрителя
FOLLOW_HINT_SCOPE = 'follow_hint'

# То же для отказов по правам и по выбранной квоте. Это неизменные факты:
# повторять их на каждое сообщение незачем, а вот спамом в чат это выходит
# быстро – у зрителя без значков собственного кулдауна нет вовсе
DENY_SCOPE = 'deny'
DENY_REPEAT_SECONDS = 30


def _status_of(chatter) -> str:
    """Статус для кулдаунов.

    Саб проверяется раньше VIP: если человек и то и другое, ему достаётся
    более щадящий кулдаун. Фаундеры канала носят свой значок вместо
    сабского, поэтому засчитываем и его.
    """
    if chatter.broadcaster:
        return STATUS_BROADCASTER
    if chatter.moderator:
        return STATUS_MODERATOR
    if chatter.subscriber or chatter.founder:
        return STATUS_SUB
    if chatter.vip:
        return STATUS_VIP
    return STATUS_REGULAR


def _quota_per_hour(status: str) -> int:
    """Потолок обращений к Gemini за окно. 0 – без лимита.

    Лестница та же, что у кулдауна: кто занёс деньги или носит значок, тот
    не ограничен ничем. Ограничение поверх кулдауна нужно против медленного,
    но постоянного выкачивания: кулдаун держит темп, квота – объём.
    """
    if status in (STATUS_BROADCASTER, STATUS_MODERATOR, STATUS_SUB):
        return 0
    if status == STATUS_VIP:
        return Quota.VIP_PER_HOUR
    return Quota.FOLLOWER_PER_HOUR


# Какой отказ показать, когда значка не хватило
ROLE_DENIED_TEXTS = {
    ROLE_VIP_MOD_BROADCASTER: 'role_denied',
    ROLE_SUB_VIP_MOD_BROADCASTER: 'role_denied_sub',
}


def _has_role(role: str | None, chatter) -> bool:
    """Хватает ли значков для команды.

    Значки проверяем напрямую, а не через _status_of(): тот схлопывает
    подписчика и VIP в один статус ради кулдауна, и сабающий випер иначе
    не прошёл бы туда, куда пускают VIP.
    """
    if role is None:
        return True
    if chatter.broadcaster or chatter.moderator or chatter.vip:
        return True
    if role == ROLE_SUB_VIP_MOD_BROADCASTER and (chatter.subscriber or chatter.founder):
        return True
    return False


def _cooldown_seconds(status: str) -> int:
    """Пауза в секундах для статуса зрителя. 0 – кулдауна нет.

    Лестница одна на оба класса команд и на свободное обращение к боту.
    Кто занёс деньги или носит значок – не ждёт вовсе.
    """
    if status in (STATUS_BROADCASTER, STATUS_MODERATOR, STATUS_SUB):
        return 0
    if status == STATUS_VIP:
        return Cooldown.VIP
    return Cooldown.REGULAR



class ChatComponent(commands.Component):

    def __init__(self, bot):
        self.bot = bot
        self._followers = FollowerCache()
        self._registry = CommandRegistry()
        add = self._registry.add
        # Все команды работают голыми, без обращения к боту.
        # Порядок важен: более длинные триггеры регистрируются раньше.
        add(HELP_TRIGGER,      handle_help)
        add(STATS_TRIGGER,     handle_stats,     prefix=True)
        add(ROLL_TRIGGER,      handle_roll)
        add(SUMMARY_TRIGGER,   handle_summary,   kind=KIND_GEMINI)
        add(WHO_TRIGGER,       handle_who,       prefix=True, kind=KIND_GEMINI)
        add(VERSUS_TRIGGER,    handle_versus,    prefix=True, kind=KIND_GEMINI)
        add(DEFACT_TRIGGER,    handle_defact,    prefix=True, role=ROLE_VIP_MOD_BROADCASTER)
        add(FACT_TRIGGER,      handle_fact,      prefix=True, role=ROLE_VIP_MOD_BROADCASTER)
        add(ASK_TRIGGER,       handle_ask,       prefix=True, kind=KIND_GEMINI)
        if Picture.ENABLED:
            # Выключенная фича не должна висеть командой, которая молча
            # ничего не делает: её просто нет
            add(ASCII_TRIGGER, handle_ascii, prefix=True, kind=KIND_GEMINI,
                role=ROLE_SUB_VIP_MOD_BROADCASTER)

    @commands.Component.listener()
    async def event_message(self, message: twitchio.ChatMessage) -> None:
        if str(message.chatter.id) == str(self.bot.bot_id):
            return
        if not self.bot.bot_name:
            return

        # session_id берём один раз: он меняется на начале и конце эфира, а корутины живут дольше
        session_id = self.bot.session_id
        # Роутинг идёт до записи: он же решает, было ли сообщение обращением к
        # боту (оклик словом, @упоминание, реплай), а это хранится вместе с
        # сообщением и считается в !stat
        entry, prompt, addressed = self._route(message)
        await save_chat_message(
            session_id, message.chatter.name, message.text, addressed=addressed,
        )
        # Первое появление в эфире запускает отсчёт бонусов игры по итогам прошлого
        await roll_perks_on_chat(self.bot, session_id, message.chatter.name)

        if entry is None and prompt is None:
            return

        user = message.chatter.name
        chatter = message.chatter
        # Свободное обращение к боту идёт в Gemini наравне с !ask, поэтому и
        # счётчик у него общий с Gemini-командами, а не с локальными.
        kind = entry.kind if entry is not None else KIND_GEMINI
        status = _status_of(chatter)
        seconds = _cooldown_seconds(status)

        # Без фолова бот не отвечает: исключение – справка, по ней человек и
        # узнаёт, ради чего фоловиться
        if status == STATUS_REGULAR and not await self._allowed_without_follow(message, user, entry):
            return

        if seconds:
            remaining = self.bot.cooldown_remaining(user, kind)
            if remaining > 0:
                key = 'cooldown_gemini' if kind == KIND_GEMINI else 'cooldown_local'
                await message.respond(
                    Content.text(key, user=user, seconds=int(remaining) + 1)
                )
                return

        if entry is not None and not _has_role(entry.role, chatter):
            await self._deny(message, user, ROLE_DENIED_TEXTS[entry.role],
                             command=entry.trigger)
            return

        # Квота поверх кулдауна: считаются только запросы к Gemini – они стоят
        # денег. Локальные команды держит один кулдаун
        if kind == KIND_GEMINI and not await self._within_quota(message, user, status):
            return

        ctx = CommandContext(
            message=message,
            user=user,
            prompt=prompt,
            original_text=message.text,
            session_id=session_id,
            bot=self.bot,
            kind=kind,
            args=entry.extract_args(prompt) if entry else '',
        )

        # Кулдаун ставится до обращения к сети – иначе быстрый спам
        # успевает запустить несколько генераций подряд.
        if seconds:
            self.bot.set_cooldown(user, seconds, kind)
        if kind == KIND_GEMINI:
            # Пишем до генерации: неудачный запрос тоже стоил очереди и денег
            await record_bot_use(user, kind)

        if entry is None:
            await handle_default(ctx)
            return
        await entry.handler(ctx)

    async def _allowed_without_follow(self, message, user: str, entry) -> bool:
        """Пускать ли незафоловленного зрителя. False – ему уже ответили отказом."""
        if not Follow.REQUIRED:
            return True
        if entry is not None and entry.trigger == HELP_TRIGGER:
            return True
        if await self._followers.is_follower(self.bot, message.chatter.id):
            return True
        # Подсказку повторяем не чаще раза в FOLLOW_HINT_MINUTES: иначе спам
        # командами превратился бы в спам отказами
        if not self.bot.cooldown_remaining(user, FOLLOW_HINT_SCOPE):
            self.bot.set_cooldown(user, Follow.HINT_MINUTES * 60, FOLLOW_HINT_SCOPE)
            await message.respond(Content.text('follow_required', user=user, command=HELP_TRIGGER))
        return False

    async def _deny(self, message, user: str, key: str, **values) -> None:
        """Отказать, но не чаще раза в DENY_REPEAT_SECONDS на зрителя.

        Отказы по правам и по квоте не меняются от повтора к повтору, а
        собственного кулдауна у зрителя без значков нет – без тормоза бот
        отвечал бы отказом на каждое его сообщение.
        """
        if self.bot.cooldown_remaining(user, DENY_SCOPE):
            return
        self.bot.set_cooldown(user, DENY_REPEAT_SECONDS, DENY_SCOPE)
        await message.respond(Content.text(key, user=user, **values))

    async def _within_quota(self, message, user: str, status: str) -> bool:
        """Не выбрал ли зритель часовую квоту. False – ему уже ответили отказом."""
        limit = _quota_per_hour(status)
        if not limit:
            return True
        used = await count_bot_uses(user, KIND_GEMINI, Quota.WINDOW_MINUTES)
        if used < limit:
            return True
        # Место освободится, когда самое раннее обращение выпадет из окна
        age = await oldest_bot_use_age(user, KIND_GEMINI, Quota.WINDOW_MINUTES) or 0
        minutes = max(1, math.ceil((Quota.WINDOW_MINUTES * 60 - age) / 60))
        await self._deny(message, user, 'quota_exceeded',
                         limit=limit, minutes=minutes, window=Quota.WINDOW_MINUTES)
        return False

    def _route(self, message: twitchio.ChatMessage):
        """Определить команду, текст запроса и было ли обращение к боту.

        Возвращает (entry, prompt, addressed). (None, None, False) – сообщение
        боту не адресовано. addressed – был ли оклик словом, @упоминание или
        реплай; голая команда обращением не считается, а «сосурити !roll» –
        считается.

        Команда узнаётся прямо в тексте: `!who ник` работает без обращения
        к боту. Аргументы берутся как написаны – триггеры обращения из них не
        вырезаются, иначе `!who securityexpert` потерял бы ник. Обращение
        (@бот, сосур*, secur*, реплай) нужно свободному тексту и команде,
        перед которой оно стоит.
        """
        text = message.text.strip()
        lowered = text.lower()

        entry = self._registry.resolve(lowered)
        if entry is not None:
            return entry, lowered, False

        bot_tag = f'@{self.bot.bot_name}'.lower()
        is_mention = bot_tag in lowered
        is_sosur = bool(SOSUR_RE.search(text))
        reply = getattr(message, 'reply', None)
        is_reply = reply is not None and str(getattr(reply, 'parent_user_id', '')) == str(self.bot.bot_id)
        if not (is_mention or is_sosur or is_reply):
            return None, None, False

        prompt = re.sub(re.escape(bot_tag), '', lowered)
        prompt = SOSUR_RE.sub('', prompt).strip()
        if not prompt:
            prompt = lowered
        return self._registry.resolve(prompt), prompt, True

    @commands.Component.listener()
    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        # Кэш мог запомнить его как не фолловера минуту назад
        self._followers.forget(payload.user.id)
        await handle_follow(self.bot, payload)
