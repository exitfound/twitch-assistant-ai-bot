from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Callable, Awaitable

if TYPE_CHECKING:
    import twitchio
    from bot import Bot

Handler = Callable[['CommandContext'], Awaitable[None]]

# Разделители между триггером и аргументами: "!who ник", "!ask: вопрос"
ARG_SEPARATORS = ' :,'

# Классы команд. Локальные обслуживаются из SQLite и текста CONTENT.md –
# стоят ноль и отвечают мгновенно. У Gemini-команд каждый вызов уходит в API:
# это деньги, ожидание и место в семафоре.
#
# Класс – он же область кулдауна: у каждого свой счётчик на пользователя,
# поэтому отсидка за !ask не мешает нажать !help-bot. Лестница по статусу зрителя
# при этом одна на оба класса, см. _cooldown_seconds() в src/core/component.py.
KIND_LOCAL = 'local'
KIND_GEMINI = 'gemini'

ROLE_VIP_MOD_BROADCASTER = 'vip_mod_broadcaster'
# Та же лесенка, но подписчик тоже проходит: !ascii открыт со значка саба
ROLE_SUB_VIP_MOD_BROADCASTER = 'sub_vip_mod_broadcaster'


@dataclasses.dataclass
class CommandContext:
    message: twitchio.ChatMessage
    user: str
    prompt: str
    original_text: str
    session_id: str
    bot: Bot
    kind: str = KIND_LOCAL
    args: str = ''

    def clear_cooldown(self) -> None:
        """Снять кулдаун со своего класса.

        Хендлер зовёт это, когда отказывает по формату: человек не получил
        ответа, значит и ждать ему не за что. Область берётся из самого
        контекста, чтобы её нельзя было перепутать.
        """
        self.bot.clear_cooldown(self.user, self.kind)

    async def refuse(self) -> None:
        """Отказ по формату: вернуть и кулдаун, и место в часовой квоте.

        Диспетчер отмечает обращение до вызова хендлера – иначе спам успел бы
        проскочить, – поэтому хендлер, который ничего не сгенерировал, обязан
        вернуть его сам.
        """
        self.clear_cooldown()
        if self.kind == KIND_GEMINI:
            from src.core.database import forget_bot_use
            await forget_bot_use(self.user, self.kind)


@dataclasses.dataclass
class CommandEntry:
    trigger: str
    handler: Handler
    prefix: bool
    role: str | None            # None = все, ROLE_VIP_MOD_BROADCASTER = VIP/мод/стример
    kind: str                   # KIND_LOCAL | KIND_GEMINI

    def match(self, prompt: str) -> bool:
        if not self.prefix:
            return prompt == self.trigger
        if not prompt.startswith(self.trigger):
            return False
        rest = prompt[len(self.trigger):]
        # Граница слова: "!who ник" – да, "!whoever" – нет
        return not rest or rest[0] in ARG_SEPARATORS

    def extract_args(self, prompt: str) -> str:
        if not self.prefix:
            return ''
        return prompt[len(self.trigger):].lstrip(ARG_SEPARATORS).strip()


class CommandRegistry:
    def __init__(self) -> None:
        self._entries: list[CommandEntry] = []

    def add(self, trigger: str, handler: Handler, *,
            prefix: bool = False,
            role: str | None = None,
            kind: str = KIND_LOCAL) -> None:
        self._entries.append(CommandEntry(
            trigger=trigger,
            handler=handler,
            prefix=prefix,
            role=role,
            kind=kind,
        ))

    def resolve(self, prompt: str) -> CommandEntry | None:
        for entry in self._entries:
            if entry.match(prompt):
                return entry
        return None
