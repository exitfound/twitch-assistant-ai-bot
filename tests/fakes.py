"""Stand-ins for the twitchio objects and the Bot that handlers touch."""
import itertools
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock


def make_chatter(name: str = 'viewer', *, broadcaster=False, moderator=False,
                 subscriber=False, founder=False, vip=False) -> SimpleNamespace:
    """The five badges the bot reads, plus a name and an id."""
    return SimpleNamespace(
        name=name, id=f'id-{name}', broadcaster=broadcaster, moderator=moderator,
        subscriber=subscriber, founder=founder, vip=vip,
    )


_message_ids = itertools.count(1)


def make_message(text: str, chatter: SimpleNamespace | None = None, *, reply=None) -> SimpleNamespace:
    return SimpleNamespace(id=f'msg-{next(_message_ids)}', text=text, chatter=chatter or make_chatter(),
                           reply=reply, respond=AsyncMock())


class FakeBot:
    """The part of Bot that handlers touch: session, stream state, cooldowns, sending."""

    def __init__(self, *, session_id: str = '2026-09-22 20:00', stream_live: bool = True) -> None:
        self.session_id = session_id
        self.stream_live = stream_live
        self.bot_id = '1000'
        self.bot_name = 'sosuryan_bot'
        self.rewards_active = False
        self.send_chat_message = AsyncMock(return_value=True)
        self._cooldowns: dict[str, float] = {}

    def cooldown_remaining(self, user: str, scope: str) -> float:
        return max(0.0, self._cooldowns.get(f'{scope}:{user}', 0.0) - time.time())

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None:
        self._cooldowns[f'{scope}:{user}'] = time.time() + seconds

    def clear_cooldown(self, user: str, scope: str) -> None:
        self._cooldowns.pop(f'{scope}:{user}', None)
