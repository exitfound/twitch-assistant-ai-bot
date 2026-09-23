"""Bot lifecycle pieces that can be checked without Twitch."""

import asyncio
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
    """event_ready fires again on a reconnect, close() runs more than once on shutdown:
    a loop starts once, is cancelled and awaited the first time, and later calls are silent."""
    tasks = BackgroundTasks()
    assert tasks.start('loop', lambda: asyncio.sleep(3600))
    assert not tasks.start('loop', lambda: asyncio.sleep(3600))
    caplog.set_level('INFO', logger='src.core.tasks')
    await tasks.stop()
    await tasks.stop()
    assert not tasks.running('loop')
    assert [r.getMessage() for r in caplog.records].count('Фоновые задачи остановлены: 1') == 1


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


def test_a_migrated_socket_stays_in_the_registry(monkeypatch):
    """On session_reconnect the new socket shares the old one's session id: closing
    the old one must not drop the new one, or the watch subscribes to chat twice."""
    monkeypatch.setattr(chat_socket.Websocket, '_cleanup', chat_socket.Websocket._cleanup)
    chat_socket.keep_migrated_sockets()
    chat_socket.keep_migrated_sockets()
    bot = bot_module.Bot()
    watch = chat_socket.ChatSocketWatch(bot, AsyncMock(), active=lambda: True)

    def socket():
        ws = chat_socket.Websocket(client=bot, token_for=str(bot.bot_id), http=bot._http)
        ws._session_id = 'session'
        return ws

    old, new = socket(), socket()
    bot._websockets[str(bot.bot_id)]['session'] = new
    old._cleanup()

    assert bot._websockets[str(bot.bot_id)] == {'session': new}
    assert watch.alive()
    new._cleanup()
    assert not watch.alive()


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
