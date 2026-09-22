"""Keeping the chat subscription alive – and every reach into twitchio's private state.

twitchio 3.2.1 can abandon an EventSub websocket for good (a welcome over 11 s, a revoked
subscription, a close code it does not retry), leaving the bot deaf. One still retrying
keeps _closed clear, so a revive is due only when none is open. twitchio exposes none of
this publicly: check_private_api() runs at startup, so an upgrade that renames the fields
stops the bot loudly instead of letting the watch silently see a dead socket as alive.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from twitchio.eventsub.websockets import Websocket
from twitchio.ext import commands

logger = logging.getLogger(__name__)

# How often the chat connection is checked
CHAT_WATCH_SECONDS = 60
# After a close event: a normal reconnect finishes within this, a given-up socket does not
CLOSED_GRACE_SECONDS = 5


def check_private_api(bot: commands.Bot) -> None:
    """Fail at startup if twitchio no longer has the private fields the bot relies on."""
    missing = []
    if not isinstance(getattr(bot, '_websockets', None), dict):
        missing.append('Client._websockets')
    if '_closed' not in Websocket.__init__.__code__.co_names:
        missing.append('Websocket._closed')
    if not isinstance(getattr(getattr(bot, '_http', None), '_tokens', None), dict):
        missing.append('ManagedHTTPClient._tokens')
    if missing:
        raise RuntimeError(f'twitchio изменился, нет полей: {", ".join(missing)} – проверь версию в requirements.in')


def stored_token(bot: commands.Bot, user_id: str) -> dict | None:
    """The token twitchio holds for a user (loaded from .tio.tokens.json, fresher after refreshes)."""
    return bot._http._tokens.get(user_id)


class ChatSocketWatch:
    """Resubscribes to chat when every EventSub socket of the bot is gone."""

    def __init__(self, bot: commands.Bot, subscribe: Callable[[], Awaitable[None]],
                 active: Callable[[], bool]) -> None:
        self._bot = bot
        self._subscribe = subscribe     # the bot's own subscription to chat and events
        self._active = active           # False while shutting down or without a channel
        self._lock = asyncio.Lock()

    def alive(self) -> bool:
        sockets = self._bot._websockets.get(str(self._bot.bot_id), {})
        return any(not socket._closed for socket in sockets.values())

    async def revive(self) -> None:
        if not self._active() or self.alive():
            return
        async with self._lock:
            if not self._active() or self.alive():
                return
            logger.warning('Соединение с чатом Twitch потеряно – подписываюсь заново')
            # Dead sockets out of the registry, or subscribe_websocket() would pick one
            self._bot._websockets.pop(str(self._bot.bot_id), None)
            await self._subscribe()
            logger.warning('Чат снова подключён')

    async def loop(self) -> None:
        logger.info('Проверка соединения с чатом включена (раз в %d с)', CHAT_WATCH_SECONDS)
        while True:
            await asyncio.sleep(CHAT_WATCH_SECONDS)
            await self._try_revive()

    async def closed(self) -> None:
        """A socket closed. Fired for every close, a normal reconnect included; a socket
        that is reconnecting stays open for alive(), so this only speeds up the watch
        when the socket was given up."""
        if not self._active():
            return
        await asyncio.sleep(CLOSED_GRACE_SECONDS)
        await self._try_revive()

    async def _try_revive(self) -> None:
        try:
            await self.revive()
        except Exception as e:
            # Twitch or the network is still down: the next check tries again
            logger.warning('Переподписка на чат не удалась: %s', e)
