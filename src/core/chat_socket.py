"""Keeping the EventSub subscriptions alive – and every reach into twitchio's private state.

twitchio 3.2.1 can abandon an EventSub websocket for good (a welcome over 11 s, a revoked
subscription, a close code it does not retry) or reconnect it without its subscriptions
(renewal refused while the network flaps), leaving the bot deaf. twitchio exposes none of
this publicly: check_private_api() runs at startup, so an upgrade that renames the fields
stops the bot loudly instead of letting the watch silently see a dead socket as alive.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from twitchio.eventsub.websockets import Websocket
from twitchio.ext import commands

logger = logging.getLogger(__name__)

# How often each watched subscription is checked
WATCH_SECONDS = 60
# After a close event: a normal reconnect finishes within this, a given-up socket does not
CLOSED_GRACE_SECONDS = 5


def check_private_api(bot: commands.Bot) -> None:
    """Fail at startup if twitchio no longer has the private fields the bot relies on."""
    missing = []
    if not isinstance(getattr(bot, '_websockets', None), dict):
        missing.append('Client._websockets')
    fields = Websocket.__init__.__code__.co_names
    missing.extend(f'Websocket.{f}' for f in ('_closed', '_subscriptions', '_connection_tasks')
                   if f not in fields)
    if not isinstance(getattr(getattr(bot, '_http', None), '_tokens', None), dict):
        missing.append('ManagedHTTPClient._tokens')
    if not callable(getattr(Websocket, '_cleanup', None)):
        missing.append('Websocket._cleanup')
    if missing:
        raise RuntimeError(f'twitchio изменился, нет полей: {", ".join(missing)} – проверь версию в requirements.in')


def keep_migrated_sockets() -> None:
    """Keep a socket that took over a session in twitchio's registry.

    On session_reconnect Twitch keeps the session id: twitchio registers the new socket
    under the old one's id, then the old one's _cleanup() pops that key. The live socket
    falls out of the registry, the watch sees none and subscribes again, and every chat
    message arrives twice.
    """
    original = Websocket._cleanup
    if getattr(original, 'keeps_successor', False):
        return

    def cleanup(self: Websocket, closed: bool = True) -> None:
        sockets = self._client._websockets.get(self._token_for, {}) if self._client else {}
        successor = sockets.get(self.session_id or '')
        original(self, closed)
        if successor is not None and successor is not self:
            sockets[self.session_id] = successor

    cleanup.keeps_successor = True
    Websocket._cleanup = cleanup


def stored_token(bot: commands.Bot, user_id: str) -> dict | None:
    """The token twitchio holds for a user (loaded from .tio.tokens.json, fresher after refreshes)."""
    return bot._http._tokens.get(user_id)


def _types(socket: Websocket) -> set[str]:
    return {getattr(sub['type'], 'value', sub['type']) for sub in socket._subscriptions.values()}


def _reconnecting(socket: Websocket) -> bool:
    return any(not task.done() for task in socket._connection_tasks)


class SocketWatch:
    """Resubscribes when no socket of a token still carries its subscription.

    One per feed: chat on the bot's token, redemptions on the channel's. A socket counts
    while it is connected and holds the watched subscription, or while twitchio is
    reconnecting it: a failed renewal leaves a socket open but deaf, and a renewal that
    died on the network leaves one that will never reconnect again.
    """

    def __init__(self, bot: commands.Bot, name: str, token_for: Callable[[], str | None],
                 sub_type: str, owned: frozenset[str], subscribe: Callable[[], Awaitable[None]],
                 active: Callable[[], bool]) -> None:
        self._bot = bot
        self._name = name               # for the log: «чат», «награды»
        self._token_for = token_for     # the key of the feed's sockets in twitchio's registry
        self._sub_type = sub_type       # e.g. channel.chat.message
        self._owned = owned | {sub_type}    # every type subscribe() creates
        self._subscribe = subscribe     # subscribes the feed anew
        self._active = active           # False while shutting down or before the feed started
        self._lock = asyncio.Lock()

    def _sockets(self) -> list[Websocket]:
        return list(self._bot._websockets.get(self._token_for() or '', {}).values())

    def _carries(self, socket: Websocket) -> bool:
        if socket._closed:
            return False
        # After a welcome twitchio renews the subscriptions one by one: mid-reconnect
        # the socket counts whatever it holds at the moment
        if _reconnecting(socket):
            return True
        return socket.connected and self._sub_type in _types(socket)

    def alive(self) -> bool:
        return any(self._carries(socket) for socket in self._sockets())

    async def revive(self) -> None:
        if not self._active() or self.alive():
            return
        async with self._lock:
            if not self._active() or self.alive():
                return
            logger.warning('Подписка «%s» потеряна – подписываюсь заново', self._name)
            # The feed's leftover sockets go: one still holding its other events would
            # deliver them twice. A socket of another feed on the same token stays
            for socket in self._sockets():
                if _types(socket) <= self._owned:
                    await _close(socket)
            sockets = self._bot._websockets.get(self._token_for() or '', {})
            for key, socket in list(sockets.items()):
                if socket._closed:
                    del sockets[key]
            await self._subscribe()
            logger.warning('Подписка «%s» восстановлена', self._name)

    async def loop(self) -> None:
        logger.info('Проверка подписки «%s» включена (раз в %d с)', self._name, WATCH_SECONDS)
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            await self._try_revive()

    async def closed(self) -> None:
        """A socket closed. Fired for every close, a normal reconnect included; a socket
        that is reconnecting still counts, so this only speeds up the watch when the
        subscription was really lost."""
        if not self._active():
            return
        await asyncio.sleep(CLOSED_GRACE_SECONDS)
        await self._try_revive()

    async def _try_revive(self) -> None:
        try:
            await self.revive()
        except Exception as e:
            # Twitch or the network is still down: the next check tries again
            logger.warning('Переподписка «%s» не удалась: %s', self._name, e)


async def _close(socket: Websocket) -> None:
    """Close a socket for good. Its pending reconnect is cancelled first: close() does
    not stop it, and a reconnect that gets through reopens the socket and takes the
    registry over from the fresh one."""
    for task in list(socket._connection_tasks):
        task.cancel()
    try:
        await socket.close()
    except Exception as e:
        logger.debug('Сокет %s не закрылся: %s', socket, e)
