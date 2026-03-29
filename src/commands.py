from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Callable, Awaitable

if TYPE_CHECKING:
    import twitchio
    from bot import Bot

Handler = Callable[['CommandContext'], Awaitable[None]]


@dataclasses.dataclass
class CommandContext:
    message: twitchio.ChatMessage
    user: str
    prompt: str
    original_text: str
    session_id: str
    bot: Bot


@dataclasses.dataclass
class CommandEntry:
    trigger: str
    handler: Handler
    prefix: bool
    role: str | None  # None = all, 'vip_mod_broadcaster' = VIP/mod/broadcaster only


class CommandRegistry:
    def __init__(self) -> None:
        self._entries: list[CommandEntry] = []

    def add(self, trigger: str, handler: Handler, *,
            prefix: bool = False,
            role: str | None = None) -> None:
        self._entries.append(CommandEntry(
            trigger=trigger,
            handler=handler,
            prefix=prefix,
            role=role,
        ))

    def resolve(self, prompt: str) -> CommandEntry | None:
        for entry in self._entries:
            if entry.prefix:
                if prompt.startswith(entry.trigger):
                    return entry
            else:
                if prompt == entry.trigger:
                    return entry
        return None
