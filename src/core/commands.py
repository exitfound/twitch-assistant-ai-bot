from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import twitchio

    from src.core.port import BotPort

Handler = Callable[['CommandContext'], Awaitable[None]]

# Separators between the trigger and the args: "!who ник", "!ask: вопрос"
ARG_SEPARATORS = ' :,'

# Command classes. Local ones answer from SQLite and CONTENT.md at no cost, a Gemini
# call costs money, waiting and a semaphore slot. The class is also the cooldown scope,
# while the status ladder is shared – see _cooldown_seconds() in src/core/component.py.
class Kind(StrEnum):
    LOCAL = 'local'
    GEMINI = 'gemini'


class Role(StrEnum):
    # From the subscriber badge up: VIP, moderator and broadcaster pass too (!ascii)
    SUB_VIP_MOD_BROADCASTER = 'sub_vip_mod_broadcaster'


@dataclasses.dataclass
class CommandContext:
    message: twitchio.ChatMessage
    user: str
    prompt: str
    original_text: str
    session_id: str
    bot: BotPort
    kind: Kind = Kind.LOCAL
    args: str = ''

    @property
    def original_args(self) -> str:
        """Command args in their original case.

        args are cut from the lowercased prompt, so the same substring is looked
        up in the original message text. Needed where case is part of the
        meaning: a fact, a question to the model.
        """
        if not self.args:
            return ''
        index = self.original_text.lower().rfind(self.args)
        if index == -1:
            return self.args
        return self.original_text[index:index + len(self.args)].strip()

    def clear_cooldown(self) -> None:
        """Release the cooldown of its own class.

        A handler calls this when it rejects malformed input: the viewer got no
        answer, so there is nothing to wait for. The scope is taken from the
        context itself, so it cannot be mixed up.
        """
        self.bot.clear_cooldown(self.user, self.kind)

    async def refuse(self) -> None:
        """Rejection of malformed input: give back both the cooldown and the hourly quota slot.

        The dispatcher records the request before calling the handler – otherwise
        spam would slip through – so a handler that generated nothing must give
        it back itself.
        """
        self.clear_cooldown()
        if self.kind == Kind.GEMINI:
            from src.core.database import forget_bot_use
            await forget_bot_use(self.user, self.kind)


@dataclasses.dataclass
class CommandEntry:
    trigger: str
    handler: Handler
    prefix: bool
    role: Role | None           # None = everyone, otherwise the badges it needs
    kind: Kind                  # Kind.LOCAL | Kind.GEMINI

    def match(self, prompt: str) -> bool:
        if not self.prefix:
            return prompt == self.trigger
        if not prompt.startswith(self.trigger):
            return False
        rest = prompt[len(self.trigger):]
        # Word boundary: "!who ник" – yes, "!whoever" – no
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
            role: Role | None = None,
            kind: Kind = Kind.LOCAL) -> None:
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
