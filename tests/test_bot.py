"""Bot lifecycle pieces that can be checked without Twitch."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import twitchio

import bot as bot_module
from fakes import FakeBot
from src.core import chat_socket, cooldowns, database, logging_setup, stream, tokens
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


def _refresh_refused() -> twitchio.HTTPException:
    """What twitchio raises when Twitch refuses a revoked refresh token."""
    route = twitchio.Route('POST', '/oauth2/token', use_id=True)
    return twitchio.HTTPException('refused', route=route, status=400,
                                  extra={'status': 400, 'message': 'Invalid refresh token'})


@pytest.mark.parametrize(('error', 'dead'), [
    (_refresh_refused(), True),
    (twitchio.InvalidTokenException('x', token='t', refresh='r', type_='refresh',
                                     original=_refresh_refused()), True),
    (twitchio.HTTPException('x', status=401, extra={'message': 'unauthorized'}), True),
    (twitchio.HTTPException('x', status=403, extra={'message': 'missing scope'}), True),
    (twitchio.HTTPException('x', route=twitchio.Route('POST', 'eventsub/subscriptions'), status=400,
                            extra={'message': 'bad transport'}), False),
    (twitchio.HTTPException('x', status=503, extra={'message': 'down'}), False),
    (aiohttp.ClientConnectionError('reset'), False),
    (TimeoutError(), False),
])
def test_only_a_revoked_authorization_counts_as_a_dead_token(error, dead):
    """Retrying a revoked token only hammers Twitch; a network error must still be retried."""
    assert tokens.token_dead(error) is dead


def test_the_log_hides_secrets():
    """twitchio's error for a refused refresh carries the client secret and the refresh token."""
    line = ('Request https://id.twitch.tv/oauth2/token?client_id=app&client_secret=s3cret'
            '&grant_type=refresh_token&refresh_token=r3fresh failed with status 400. '
            'Please re-authenticate user with token: "acc3ss" {\'token\': \'t0ken\', \'refresh\': \'r3\'}')
    record = logging.LogRecord('twitchio', logging.WARNING, __file__, 1, line, None, None)
    text = logging_setup.make_formatter().format(record)
    for secret in ('s3cret', 'r3fresh', 'acc3ss', 't0ken', "'r3'"):
        assert secret not in text
    assert 'client_id=app' in text and 'grant_type=refresh_token&' in text


def _token_holder(stored: dict) -> SimpleNamespace:
    return SimpleNamespace(bot_id='1000', _http=SimpleNamespace(_tokens=stored), add_token=AsyncMock())


async def test_a_stored_bot_token_wins_over_env(monkeypatch):
    """A new login lands in .tio.tokens.json; the stale .env token must not replace it."""
    monkeypatch.setattr(tokens.Twitch, 'BOT_TOKEN', 'old')
    monkeypatch.setattr(tokens.Twitch, 'BOT_REFRESH', 'old-refresh')
    holder = _token_holder({'1000': {'token': 'fresh', 'refresh': 'fresh-refresh'}})
    await tokens.add_bot_token(holder)
    holder.add_token.assert_not_awaited()


async def test_env_bot_token_is_the_fallback(monkeypatch):
    monkeypatch.setattr(tokens.Twitch, 'BOT_TOKEN', 'env')
    monkeypatch.setattr(tokens.Twitch, 'BOT_REFRESH', 'env-refresh')
    holder = _token_holder({})
    await tokens.add_bot_token(holder)
    holder.add_token.assert_awaited_once_with('env', 'env-refresh')


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


async def test_a_failed_resubscription_does_not_retry_on_its_own_close(monkeypatch):
    """A failed attempt closes its fresh socket; retrying on every close would loop, one more
    chain each round. Only the minute check retries after a failure."""
    monkeypatch.setattr(chat_socket, 'CLOSED_GRACE_SECONDS', 0)
    bot = bot_module.Bot()
    subscribe = AsyncMock(side_effect=aiohttp.ClientConnectionError('down'))
    watch = _watch(bot, subscribe)

    await asyncio.gather(*(watch.closed() for _ in range(5)))
    assert subscribe.await_count == 1
    await watch.closed()
    assert subscribe.await_count == 1

    await watch._try_revive()
    assert subscribe.await_count == 2
    subscribe.side_effect = None
    await watch._try_revive()
    assert subscribe.await_count == 3
    # Recovered: a close is checked at once again
    await watch.closed()
    assert subscribe.await_count == 4


async def test_a_dead_token_goes_to_the_failure_hook(caplog):
    bot = bot_module.Bot()
    on_failure = AsyncMock(return_value=True)
    watch = chat_socket.SocketWatch(bot, 'награды', lambda: str(bot.bot_id), REDEEM, frozenset(),
                                    AsyncMock(side_effect=_refresh_refused()), active=lambda: True,
                                    on_failure=on_failure)
    await watch._try_revive()
    on_failure.assert_awaited_once()
    assert 'Переподписка' not in caplog.text


@pytest.fixture
def live_bot(monkeypatch):
    """A Bot with rewards on, the channel known, the stream live and the chat recorded."""
    monkeypatch.setattr(bot_module.Rewards, 'ENABLED', True)
    monkeypatch.setattr(bot_module.Bot, 'stream_live', True)
    monkeypatch.setattr(bot_module.Bot, 'session_id', '2026-09-30 20:05')
    bot = bot_module.Bot()
    bot._channel_id = '458646238'
    bot.send_chat_message = AsyncMock(return_value=True)
    return bot


async def test_a_revoked_channel_token_stops_the_rewards_and_tells_the_chat_once(live_bot):
    """Without the channel's token the rewards stay open on Twitch but do nothing: the watch
    stops retrying and the chat learns why."""
    live_bot._broadcaster_token = True
    live_bot._rewards.active = True

    assert await live_bot._rewards_failed(_refresh_refused())

    assert not live_bot._rewards.active
    assert not live_bot._broadcaster_token
    assert not live_bot._rewards_watch._active()
    live_bot.send_chat_message.assert_awaited_once_with('texts.rewards_down')
    await live_bot.tell_rewards_down()
    live_bot.send_chat_message.assert_awaited_once()


async def test_a_failed_reward_start_is_retried_until_it_works(live_bot, monkeypatch):
    """A Twitch blip at start left the rewards dead, and maybe open, until a restart."""
    monkeypatch.setattr(bot_module, 'REWARDS_RETRY_SECONDS', 0)
    live_bot._broadcaster_token = True
    blips = [aiohttp.ClientConnectionError('down'), aiohttp.ClientConnectionError('down')]
    started = asyncio.Event()

    async def start(channel_id, *, open_):
        if blips:
            raise blips.pop()
        live_bot._rewards.active = True
        started.set()
    monkeypatch.setattr(live_bot._rewards, 'start', start)

    await live_bot._start_rewards()
    assert not live_bot._rewards.active
    async with asyncio.timeout(2):
        await started.wait()
    assert not live_bot._tasks.running('rewards_retry')
    live_bot.send_chat_message.assert_not_awaited()


async def test_a_dead_token_at_start_is_not_retried(live_bot, monkeypatch):
    live_bot._broadcaster_token = True
    monkeypatch.setattr(live_bot._rewards, 'start', AsyncMock(side_effect=_refresh_refused()))
    await live_bot._start_rewards()
    assert not live_bot._tasks.running('rewards_retry')
    assert not live_bot._broadcaster_token
    live_bot.send_chat_message.assert_awaited_once_with('texts.rewards_down')


async def test_a_network_error_leaves_the_rewards_alone(live_bot):
    live_bot._broadcaster_token = True
    live_bot._rewards.active = True
    assert not await live_bot._rewards_failed(aiohttp.ClientConnectionError('down'))
    assert live_bot._rewards.active
    live_bot.send_chat_message.assert_not_awaited()


async def test_the_chat_hears_of_dead_rewards_once_per_stream(live_bot, monkeypatch):
    await live_bot.tell_rewards_down()
    await live_bot.tell_rewards_down()
    assert live_bot.send_chat_message.await_count == 1
    monkeypatch.setattr(bot_module.Bot, 'session_id', '2026-10-01 20:00')
    await live_bot.tell_rewards_down()
    assert live_bot.send_chat_message.await_count == 2


async def test_working_rewards_or_an_offline_stream_say_nothing(live_bot, monkeypatch):
    live_bot._broadcaster_token = True
    await live_bot.tell_rewards_down()
    live_bot._broadcaster_token = False
    monkeypatch.setattr(bot_module.Bot, 'stream_live', False)
    await live_bot.tell_rewards_down()
    live_bot.send_chat_message.assert_not_awaited()


def _posting_bot(response) -> bot_module.Bot:
    """A Bot whose Helix chat call answers with `response`."""
    bot = bot_module.Bot()
    bot._channel_id = '458646238'
    bot._http = SimpleNamespace(post_chat_message=AsyncMock(return_value=response))
    return bot


async def test_a_line_twitch_dropped_is_not_reported_as_sent(caplog):
    """Helix answers 200 for a dropped line and says so in is_sent: a duplicate !ascii
    within 30 s would otherwise be charged to the viewer and saved as shown."""
    bot = _posting_bot({'data': [{'message_id': '', 'is_sent': False,
                                  'drop_reason': {'code': 'msg_duplicate', 'message': '…'}}]})
    assert await bot.send_chat_message('⣿⣿⣿') is False
    assert 'msg_duplicate' in caplog.text


@pytest.mark.parametrize('response', [
    {'data': [{'message_id': 'abc', 'is_sent': True}]},
    {'data': []},
    None,
])
async def test_a_line_twitch_took_is_sent(response):
    """An answer without is_sent is taken as sent: the error was not raised."""
    assert await _posting_bot(response).send_chat_message('привет') is True


async def test_every_line_is_defused_before_it_leaves():
    bot = _posting_bot({'data': [{'is_sent': True}]})
    await bot.send_chat_message('/ban someone')
    assert bot._http.post_chat_message.await_args.kwargs['message'] == 'ban someone'


async def test_an_unheard_rewards_notice_is_repeated(live_bot):
    live_bot.send_chat_message.return_value = False
    await live_bot.tell_rewards_down()
    live_bot.send_chat_message.return_value = True
    await live_bot.tell_rewards_down()
    await live_bot.tell_rewards_down()
    assert live_bot.send_chat_message.await_count == 2


# --- stream and shutdown: when the rewards open and pause -------------------------

@pytest.fixture
def lifecycle(live_bot, monkeypatch):
    """live_bot with the stream tracker, the rewards and the perks recording one journal."""
    calls: list = []

    def record(name):
        async def call(*args, **kwargs):
            calls.append((name, *args))
        return call
    live_bot.stream = SimpleNamespace(online=record('stream.online'), offline=record('stream.offline'))
    monkeypatch.setattr(live_bot._rewards, 'set_open', record('rewards.set_open'))
    monkeypatch.setattr(live_bot._rewards, 'stop', record('rewards.stop'))
    monkeypatch.setattr(bot_module.perks, 'on_stream_start', record('perks'))
    monkeypatch.setattr(live_bot, 'stop_background_tasks', record('tasks.stop'))
    monkeypatch.setattr(bot_module.commands.Bot, 'close', record('twitchio.close'))
    return live_bot, calls


async def test_shutdown_pauses_the_rewards_before_twitchio_closes(lifecycle):
    """A redemption made while the bot is away takes the points and nobody applies it;
    the pause needs the HTTP session that twitchio's close() ends."""
    bot, calls = lifecycle
    await bot.close()
    assert [c[0] for c in calls] == ['tasks.stop', 'rewards.stop', 'twitchio.close']
    assert bot._shutting_down


async def test_a_stream_end_pauses_the_rewards(lifecycle):
    bot, calls = lifecycle
    await bot.stream_went_offline()
    assert calls == [('stream.offline',), ('rewards.set_open', False)]


async def test_a_stream_start_opens_the_rewards_and_hands_out_perks(lifecycle):
    bot, calls = lifecycle
    await bot.stream_went_online('s1', 1000.0)
    assert ('stream.online', 's1', 1000.0) in calls
    assert ('rewards.set_open', True) in calls
    assert ('perks', bot, bot.session_id) in calls


@pytest.mark.parametrize(('kind', 'started'), [('live', True), ('rerun', False), ('premiere', False)])
async def test_only_a_live_stream_starts_a_session(lifecycle, kind, started):
    """A rerun is not a stream with the streamer: no session, no rewards, no perks."""
    bot, calls = lifecycle
    payload = SimpleNamespace(type=kind, id='s1', started_at=SimpleNamespace(timestamp=lambda: 1000.0))
    await bot.event_stream_online(payload)
    assert bool(calls) is started


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


def test_the_follow_gate_gets_what_it_needs():
    """The gate hands the bot to FollowerCache, which needs the channel and a Helix user:
    a fake without them fails open and passes every follow test by accident."""
    needed = {'channel_id', 'bot_id', 'create_partialuser'}
    assert [m for m in needed if not hasattr(FakeBot(), m)] == []
    assert [m for m in needed if not hasattr(bot_module.Bot, m)] == []
