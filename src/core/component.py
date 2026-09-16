"""Компонент чата: приём сообщений, диспетчер команд, follow-события.

Единственное место в core, которое знает про фичи: здесь команды из
src/gemini, src/local и src/local/roll попадают в реестр.
"""
import logging
import re

import twitchio
from twitchio.ext import commands

from src.core.commands import (
    KIND_GEMINI, ROLE_VIP_MOD_BROADCASTER,
    CommandContext, CommandRegistry,
)
from src.core.config import Cooldown
from src.core.content import Content
from src.core.database import save_chat_message
from src.gemini.commands import (
    handle_ask, handle_default, handle_summary, handle_versus, handle_who,
)
from src.local.commands import handle_defact, handle_fact, handle_help, handle_stats
from src.local.follow import handle_follow
from src.local.roll.command import handle_roll
from src.local.roll.perks import on_chat as roll_perks_on_chat

logger = logging.getLogger(__name__)

FACT_TRIGGER = '!fact'
DEFACT_TRIGGER = '!defact'
STATS_TRIGGER = '!stat'
HELP_TRIGGER = '!help'
ASK_TRIGGER = '!ask'
SUMMARY_TRIGGER = '!summary'
WHO_TRIGGER = '!who'
VERSUS_TRIGGER = '!versus'
ROLL_TRIGGER = '!roll'

# Обращение к боту словом: «сосур» кириллицей и «secur» латиницей.
# Новый вариант добавляется одной строкой в список.
SOSUR_VARIANTS = ('сосур', 'secur')
# Статус зрителя — от него зависит длительность кулдауна
STATUS_BROADCASTER = 'broadcaster'
STATUS_MODERATOR = 'moderator'
STATUS_SUB = 'subscriber'
STATUS_VIP = 'vip'
STATUS_REGULAR = 'regular'


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


def _cooldown_seconds(status: str) -> int:
    """Пауза в секундах для статуса зрителя. 0 — кулдауна нет.

    Лестница одна на оба класса команд и на свободное обращение к боту.
    Кто занёс деньги или носит значок — не ждёт вовсе.
    """
    if status in (STATUS_BROADCASTER, STATUS_MODERATOR, STATUS_SUB):
        return 0
    if status == STATUS_VIP:
        return Cooldown.VIP
    return Cooldown.REGULAR


SOSUR_RE = re.compile(
    r'(?:{})\w*'.format('|'.join(SOSUR_VARIANTS)), re.IGNORECASE | re.UNICODE
)


class ChatComponent(commands.Component):

    def __init__(self, bot):
        self.bot = bot
        self._registry = CommandRegistry()
        add = self._registry.add
        # Все команды работают голыми, без обращения к боту.
        # Порядок важен: более длинные триггеры регистрируются раньше.
        add(HELP_TRIGGER,      handle_help)
        add(STATS_TRIGGER,     handle_stats)
        add(ROLL_TRIGGER,      handle_roll)
        add(SUMMARY_TRIGGER,   handle_summary,   kind=KIND_GEMINI)
        add(WHO_TRIGGER,       handle_who,       prefix=True, kind=KIND_GEMINI)
        add(VERSUS_TRIGGER,    handle_versus,    prefix=True, kind=KIND_GEMINI)
        add(DEFACT_TRIGGER,    handle_defact,    prefix=True, role=ROLE_VIP_MOD_BROADCASTER)
        add(FACT_TRIGGER,      handle_fact,      prefix=True, role=ROLE_VIP_MOD_BROADCASTER)
        add(ASK_TRIGGER,       handle_ask,       prefix=True, kind=KIND_GEMINI)

    @commands.Component.listener()
    async def event_message(self, message: twitchio.ChatMessage) -> None:
        if str(message.chatter.id) == str(self.bot.bot_id):
            return
        if not self.bot.bot_name:
            return

        # session_id берём один раз: в полночь он меняется, а корутины живут дольше
        session_id = self.bot.session_id
        await save_chat_message(session_id, message.chatter.name, message.text)
        # Первое появление в эфире запускает отсчёт бонусов игры по итогам прошлого
        await roll_perks_on_chat(self.bot, session_id, message.chatter.name)

        entry, prompt = self._route(message)
        if entry is None and prompt is None:
            return

        user = message.chatter.name
        chatter = message.chatter
        # Для доступа к командам важны именно эти три значка, саб их не заменяет
        is_privileged = chatter.broadcaster or chatter.moderator or chatter.vip
        # Свободное обращение к боту идёт в Gemini наравне с !ask, поэтому и
        # счётчик у него общий с Gemini-командами, а не с локальными.
        kind = entry.kind if entry is not None else KIND_GEMINI
        seconds = _cooldown_seconds(_status_of(chatter))

        if seconds:
            remaining = self.bot.cooldown_remaining(user, kind)
            if remaining > 0:
                key = 'cooldown_gemini' if kind == KIND_GEMINI else 'cooldown_local'
                await message.respond(
                    Content.text(key, user=user, seconds=int(remaining) + 1)
                )
                return

        if entry is not None and entry.role == ROLE_VIP_MOD_BROADCASTER and not is_privileged:
            await message.respond(
                Content.text('role_denied', user=user, command=entry.trigger)
            )
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

        # Кулдаун ставится до обращения к сети — иначе быстрый спам
        # успевает запустить несколько генераций подряд.
        if seconds:
            self.bot.set_cooldown(user, seconds, kind)

        if entry is None:
            await handle_default(ctx)
            return
        await entry.handler(ctx)

    def _route(self, message: twitchio.ChatMessage):
        """Определить команду и текст запроса.

        Возвращает (entry, prompt). (None, None) — сообщение боту не адресовано.

        Команда узнаётся прямо в тексте: `!who ник` работает без обращения
        к боту. Аргументы берутся как написаны — триггеры обращения из них не
        вырезаются, иначе `!who securityexpert` потерял бы ник. Обращение
        (@бот, сосур*, secur*, реплай) нужно свободному тексту и команде,
        перед которой оно стоит.
        """
        text = message.text.strip()
        lowered = text.lower()

        entry = self._registry.resolve(lowered)
        if entry is not None:
            return entry, lowered

        bot_tag = f'@{self.bot.bot_name}'.lower()
        is_mention = bot_tag in lowered
        is_sosur = bool(SOSUR_RE.search(text))
        reply = getattr(message, 'reply', None)
        is_reply = reply is not None and str(getattr(reply, 'parent_user_id', '')) == str(self.bot.bot_id)
        if not (is_mention or is_sosur or is_reply):
            return None, None

        prompt = re.sub(re.escape(bot_tag), '', lowered)
        prompt = SOSUR_RE.sub('', prompt).strip()
        if not prompt:
            prompt = lowered
        return self._registry.resolve(prompt), prompt

    @commands.Component.listener()
    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        await handle_follow(self.bot, payload)
