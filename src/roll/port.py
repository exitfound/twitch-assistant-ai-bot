"""What the game needs from the bot, as a protocol.

The game sees the bot only through RollPort. In the service it is RemoteBot over the bot's
internal API (src/roll/remote.py); in tests – tests/fakes.py's FakeBot.
"""
from typing import Protocol


class RollPort(Protocol):
    """The session, the stream state, sending to chat, the chat list and the game's own cooldowns."""

    @property
    def session_id(self) -> str: ...

    @property
    def stream_live(self) -> bool: ...

    @property
    def rewards_active(self) -> bool: ...

    async def send_chat_message(self, text: str) -> bool: ...

    async def fetch_chatters(self) -> set[str] | None: ...

    def cooldown_remaining(self, user: str, scope: str) -> float: ...

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None: ...
