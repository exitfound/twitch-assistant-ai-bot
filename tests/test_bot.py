"""Bot lifecycle pieces that can be checked without Twitch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import twitchio

import bot as bot_module
from fakes import FakeBot
from src.core import chat_socket, cooldowns, database, stream, tokens
from src.core.tasks import BackgroundTasks
from src.core.port import BotPort, StreamBot


@pytest.mark.parametrize(('error', 'hint'), [
    (twitchio.HTTPException('x', status=401, extra={'message': 'invalid token'}), True),
    (twitchio.HTTPException('x', status=403, extra={'message': 'missing scope'}), True),
    (twitchio.HTTPException('x', status=409, extra={'message': 'already subscribed'}), False),
    (twitchio.HTTPException('x', status=503, extra={'message': 'down'}), False),
    (aiohttp.ClientConnectionError('reset'), False),
    (TimeoutError(), False),
    (KeyError('no token for this user'), True),
])
def test_oauth_hint_only_for_token_problems(error, hint):
    """A network error or a 5xx printed «no token, log in here» and sent the owner to
    re-authorise for nothing."""
    assert tokens.token_problem(error) is hint


async def test_closed_database_is_not_reopened_behind_the_shutdown(db):
    """A task finishing after close_db() would open a new connection, and its non-daemon
    thread would keep the process from exiting."""
    await database.close_db()
    with pytest.raises(RuntimeError):
        await database.get_db()
    await database.init_db()
    assert await database.get_db() is not None


async def test_background_tasks_start_once_and_stop_quietly_after(caplog):
    """close() runs more than once on shutdown: a loop starts once, is cancelled and
    awaited the first time, and later calls are silent."""
    tasks = BackgroundTasks()
    assert tasks.start('loop', lambda: asyncio.sleep(3600))
    assert not tasks.start('loop', lambda: asyncio.sleep(3600))
    caplog.set_level('INFO', logger='src.core.tasks')
    await tasks.stop()
    await tasks.stop()
    assert not tasks.running('loop')
    assert [r.getMessage() for r in caplog.records].count('Фоновые задачи остановлены: 1') == 1


async def test_a_loop_that_dies_is_logged(caplog):
    """Loops run until cancelled: one that fails is gone until restart and must say so."""
    async def broken():
        raise RuntimeError('boom')
    tasks = BackgroundTasks()
    tasks.start('broken', broken)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any('broken упала' in r.getMessage() for r in caplog.records)


def test_cooldowns_ignore_a_jump_of_the_system_clock(monkeypatch):
    """time.time() jumps with NTP or a host suspend: a 30-second wait must stay 30 seconds."""
    now = [1000.0]
    monkeypatch.setattr(cooldowns.time, 'monotonic', lambda: now[0])
    store = cooldowns.Cooldowns()
    store.set('gop', 30, 'gemini')
    monkeypatch.setattr(cooldowns.time, 'time', lambda: 10**10)
    assert store.remaining('gop', 'gemini') == 30
    assert store.remaining('gop', 'local') == 0
    now[0] += 31
    assert store.remaining('gop', 'gemini') == 0


def test_twitchio_still_has_the_private_fields_the_bot_uses():
    """The pinned twitchio passes; an upgrade that renames them must stop the bot at start."""
    chat_socket.check_private_api(bot_module.Bot())
    with pytest.raises(RuntimeError, match='_websockets'):
        chat_socket.check_private_api(object())


CHAT = 'channel.chat.message'
FOLLOW = 'channel.follow'
REDEEM = 'channel.channel_points_custom_reward_redemption.add'


def _watch(bot, subscribe=None) -> chat_socket.SocketWatch:
    return chat_socket.SocketWatch(bot, 'чат', lambda: str(bot.bot_id), CHAT, frozenset({FOLLOW}),
                                   subscribe or AsyncMock(), active=lambda: True)


def _socket(bot, session: str, *sub_types: str, connected: bool = True):
    """A registered socket holding subscriptions, as twitchio stores them."""
    ws = chat_socket.Websocket(client=bot, token_for=str(bot.bot_id), http=bot._http)
    ws._session_id = session
    ws._socket = SimpleNamespace(closed=not connected)
    ws._subscriptions = {f'{session}-{t}': {'type': SimpleNamespace(value=t)} for t in sub_types}
    bot._websockets[str(bot.bot_id)][session] = ws
    return ws


@pytest.fixture
def closed(monkeypatch):
    """Websocket.close() replaced: records the socket and marks it closed, no network."""
    sockets = []

    async def close(self, *args, **kwargs):
        self._closed = True
        sockets.append(self)
    monkeypatch.setattr(chat_socket.Websocket, 'close', close)
    return sockets


def test_a_migrated_socket_stays_in_the_registry(monkeypatch):
    """On session_reconnect the new socket shares the old one's session id: closing
    the old one must not drop the new one, or the watch subscribes to chat twice."""
    monkeypatch.setattr(chat_socket.Websocket, '_cleanup', chat_socket.Websocket._cleanup)
    chat_socket.keep_migrated_sockets()
    chat_socket.keep_migrated_sockets()
    bot = bot_module.Bot()
    watch = _watch(bot)
    old, new = _socket(bot, 'session', CHAT), _socket(bot, 'session', CHAT)
    new._subscriptions = old._subscriptions
    bot._websockets[str(bot.bot_id)]['session'] = new
    old._cleanup()

    assert bot._websockets[str(bot.bot_id)] == {'session': new}
    assert watch.alive()
    new._cleanup()
    assert not watch.alive()


def test_an_open_socket_without_the_subscription_is_deaf():
    """twitchio keeps a socket open after failing to renew its subscriptions."""
    bot = bot_module.Bot()
    watch = _watch(bot)
    _socket(bot, 'follows-only', FOLLOW)
    assert not watch.alive()
    _socket(bot, 'chat', CHAT)
    assert watch.alive()


async def test_a_socket_mid_reconnect_counts_whatever_it_holds():
    """After a welcome twitchio clears the subscriptions and renews them one by one."""
    bot = bot_module.Bot()
    watch = _watch(bot)
    ws = _socket(bot, 'renewing', connected=False)
    task = asyncio.create_task(asyncio.sleep(10))
    ws._connection_tasks.add(task)
    assert watch.alive()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not watch.alive()


def test_a_disconnected_socket_that_no_longer_reconnects_is_dead():
    """A renewal that died on the network leaves twitchio unable to reconnect the socket,
    while its chat subscription is still on the books."""
    bot = bot_module.Bot()
    watch = _watch(bot)
    _socket(bot, 'stuck', CHAT, connected=False)
    assert not watch.alive()


async def test_revive_closes_the_feeds_leftovers_and_cancels_their_reconnect(closed):
    """A leftover socket still holding follows would deliver them twice next to a fresh one,
    and a reconnect left running would reopen it and push the fresh one out of the registry."""
    bot = bot_module.Bot()
    subscribe = AsyncMock()
    watch = _watch(bot, subscribe)
    leftover = _socket(bot, 'follows-only', FOLLOW, connected=False)
    pending = asyncio.create_task(asyncio.sleep(10))
    leftover._connection_tasks.add(pending)
    # The reconnect gave up between the check and the close
    watch._carries = lambda socket: False

    await watch.revive()
    await asyncio.gather(pending, return_exceptions=True)

    assert closed == [leftover]
    assert pending.cancelled()
    subscribe.assert_awaited_once()
    assert bot._websockets[str(bot.bot_id)] == {}


async def test_revive_leaves_another_feeds_socket_on_the_same_token(closed):
    """With one account for the bot and the channel both watches share the token: closing
    the other feed's socket would start the two reviving each other forever."""
    bot = bot_module.Bot()
    watch = _watch(bot)
    rewards = _socket(bot, 'rewards', REDEEM)
    await watch.revive()
    assert closed == []
    assert bot._websockets[str(bot.bot_id)] == {'rewards': rewards}


@pytest.mark.parametrize(('raw', 'seconds'), [('3h8m33s', 11313), ('45m', 2700), ('9s', 9), ('', 0)])
def test_vod_duration(raw, seconds):
    assert stream.duration_seconds(raw) == seconds


def _members(protocol) -> set[str]:
    return {n for n in dir(protocol) if not n.startswith('_')} | set(protocol.__annotations__)


def test_the_bot_and_the_fake_satisfy_the_port():
    """Features see the bot only through BotPort: a member renamed on one side and not
    the other would fail at run time, in chat, not here."""
    fake = FakeBot()
    assert [m for m in _members(BotPort) if not hasattr(fake, m)] == []
    # stream is set in __init__, the class alone does not carry it
    assert [m for m in _members(StreamBot) - {'stream'} if not hasattr(bot_module.Bot, m)] == []
