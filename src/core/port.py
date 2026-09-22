"""What the features need from the bot, as protocols.

Handlers, background loops and reward outcomes see the bot only through BotPort, so
core and the features never import bot.py, and a test fake is anything with these
members (tests/fakes.py). The real Bot in bot.py satisfies both protocols.
"""
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # stream.py types its watch loop with StreamBot: a runtime import would be circular
    from src.core.stream import StreamTracker


class BotPort(Protocol):
    """The session, the stream state, sending to chat and the cooldown store."""

    @property
    def session_id(self) -> str: ...

    @property
    def stream_live(self) -> bool: ...

    @property
    def bot_id(self) -> str | None: ...

    @property
    def bot_name(self) -> str | None: ...

    @property
    def rewards_active(self) -> bool: ...

    async def send_chat_message(self, text: str) -> bool: ...

    def cooldown_remaining(self, user: str, scope: str) -> float: ...

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None: ...

    def clear_cooldown(self, user: str, scope: str) -> None: ...


class StreamBot(BotPort, Protocol):
    """BotPort plus what the stream watch drives: the tracker and the transitions."""

    stream: 'StreamTracker'

    async def fetch_live_stream(self) -> tuple[str, float] | None: ...

    async def stream_went_online(self, stream_id: str, started_at: float) -> None: ...

    async def stream_went_offline(self) -> None: ...
