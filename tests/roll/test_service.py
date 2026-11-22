"""The bot and the game as separate services: every call over real HTTP, both APIs served.

The bot's side is FakeBot behind its internal API (src/twitch/core/api.py); the game is
RemoteBot behind the game's API (src/roll/service.py); RollClient links them.
"""
import asyncio
import contextlib
import logging

import pytest
from aiohttp.test_utils import TestServer

from fakes import FakeBot, chat_component, make_chatter, make_message
from src.core.config import Follow, Service
from src.core.service import ServiceClient, ServiceUnavailable
from src.roll import game
from src.roll.remote import RemoteBot
from src.roll.service import GameService
from src.roll.specs import specs
from src.roll.storage import get_tier, save_action
from src.twitch.core.api import BotApi
from src.twitch.core.commands import PUBLIC_SCOPE, CommandContext
from src.twitch.core.followers import FollowerCache
from src.twitch.roll.client import RollClient, RollUnavailable


@pytest.fixture
async def link(db, monkeypatch):
    """(FakeBot, RollClient, RemoteBot): the bot's API and the game's API on local ports."""
    bot = FakeBot()
    api = BotApi(bot, FollowerCache())
    bot_server = TestServer(api.app())
    await bot_server.start_server()
    remote = RemoteBot(ServiceClient('бот', str(bot_server.make_url(''))))
    roll_server = TestServer(GameService(remote).app())
    await roll_server.start_server()
    monkeypatch.setattr(Service, 'ROLL_URL', str(roll_server.make_url('')).rstrip('/'))
    client = RollClient(bot, api)
    yield bot, client, remote
    await client.close()
    await remote._client.close()
    await roll_server.close()
    await bot_server.close()


def _ctx(bot: FakeBot, text: str, user: str = 'gop') -> CommandContext:
    args = text.partition(' ')[2]
    return CommandContext(message=make_message(text, make_chatter(user)), user=user, prompt=text,
                          original_text=text, session_id=bot.session_id, bot=bot, args=args)


async def test_a_throw_goes_to_the_game_and_its_reply_comes_back_to_the_message(link):
    bot, client, remote = link
    ctx = _ctx(bot, '!roll')
    await client.command('!roll', ctx)
    ctx.message.respond.assert_awaited_once()
    assert ctx.message.respond.await_args.args[0].startswith('texts.roll_')
    # The game took the session and the stream state from the call
    assert remote.session_id == bot.session_id and remote.stream_live


async def test_what_the_game_did_not_use_goes_back_to_the_gate(link):
    """Offline nothing is thrown: the reply says so and the cooldown is given back."""
    bot, client, _ = link
    bot.stream_live = False
    ctx = _ctx(bot, '!roll')
    bot.set_cooldown('gop', 30, ctx.kind)
    await client.command('!roll', ctx)
    assert ctx.message.respond.await_args.args[0] == 'texts.roll_offline'
    assert not bot.cooldown_remaining('gop', ctx.kind)


async def test_the_reward_specs_come_from_the_game(link):
    _, client, _ = link
    got = await client.specs()
    assert [spec.title for spec in got] == [spec.title for spec in specs()]


async def test_a_redemption_is_decided_by_the_game_and_said_through_the_bot(link, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    bot, client, _ = link
    assert await client.redeem(game.Action.SHIELD, 'r-1', 'gop', '42', '') is True
    bot.send_chat_message.assert_awaited()
    # A redelivery is left alone
    assert await client.redeem(game.Action.SHIELD, 'r-1', 'gop', '42', '') is None


async def test_the_journal_tells_what_was_applied(link):
    bot, client, _ = link
    await save_action('applied', bot.session_id, game.Action.EXTRA, 'gop', '', 'gop', None, 50, game.Status.OK)
    await save_action('refused', bot.session_id, game.Action.CURSE, 'gop', 'x', None, None, None,
                      game.Status.UNKNOWN_TARGET)
    assert await client.applied(['applied', 'refused', 'unseen']) == {'applied'}


async def test_chat_events_reach_the_game_in_order(link):
    bot, client, _ = link
    task = asyncio.create_task(client.events_loop())
    try:
        await client.chat(bot.session_id, 'gop', 'vip')
        await client.chat(bot.session_id, 'gop', 'subscriber')
        await asyncio.wait_for(client._events.join(), 3)
        assert await get_tier(bot.session_id, 'gop') == 'subscriber'
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_game_that_is_down_is_unavailable_and_the_viewer_hears_it(db, monkeypatch):
    """Nothing listens on the port: the command is refused with a word, the pause goes back."""
    monkeypatch.setattr(Service, 'ROLL_URL', 'http://127.0.0.1:9')
    bot = FakeBot()
    client = RollClient(bot, BotApi(bot, FollowerCache()))
    with pytest.raises(RollUnavailable):
        await client.command('!roll', _ctx(bot, '!roll'))
    with pytest.raises(RollUnavailable):
        await client.specs()
    await client.close()

    client = RollClient(bot, BotApi(bot, FollowerCache()))
    component = chat_component(bot)
    component._roll = client
    message = make_message('!roll', make_chatter('gop'))
    await component.event_message(message)
    assert message.respond.await_args.args[0] == 'texts.roll_unavailable'
    # The board's shared pause goes back too: the next viewer may ask again at once
    await component.event_message(make_message('!rollboard', make_chatter('other')))
    assert not bot.cooldown_remaining('!rollboard', PUBLIC_SCOPE)
    await client.close()


async def test_with_a_token_a_call_without_it_is_refused(db, monkeypatch):
    monkeypatch.setattr(Service, 'TOKEN', 'secret')
    bot = FakeBot()
    server = TestServer(BotApi(bot, FollowerCache()).app())
    await server.start_server()
    url = str(server.make_url(''))
    stranger, own = ServiceClient('бот', url), ServiceClient('бот', url, 'secret')
    try:
        with pytest.raises(ServiceUnavailable):
            await stranger.call('GET', '/state')
        assert (await own.call('GET', '/state'))['session_id'] == bot.session_id
    finally:
        await stranger.close()
        await own.close()
        await server.close()


def test_the_remote_bot_and_the_fake_satisfy_the_game_port():
    """The game sees the bot only through RollPort: the service's RemoteBot and the tests'
    FakeBot must both have every member."""
    from src.roll.port import RollPort
    members = {n for n in dir(RollPort) if not n.startswith('_')}
    remote = RemoteBot(ServiceClient('бот', 'http://bot.test'))
    assert members <= set(dir(remote))
    assert members <= set(dir(FakeBot()))


async def test_the_game_keeps_the_stream_state_fresh_by_itself(link, monkeypatch):
    """A stream end the game missed: its own check picks it up without a call from the bot."""
    from src.roll import remote as remote_module
    bot, _, remote = link
    assert await remote.sync() and remote.stream_live
    bot.stream_live = False
    monkeypatch.setattr(remote_module, 'STATE_SECONDS', 0)
    synced, real = asyncio.Event(), remote.sync

    async def sync():
        answered = await real()
        synced.set()
        return answered
    monkeypatch.setattr(remote, 'sync', sync)
    task = asyncio.create_task(remote.state_loop())
    try:
        await asyncio.wait_for(synced.wait(), 3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not remote.stream_live


async def test_the_game_declares_its_commands(link):
    from src.roll.service import COMMANDS
    _, client, _ = link
    got = await client.commands()
    assert [(c.trigger, c.public) for c in got] == [(c.trigger, c.public) for c in COMMANDS]


async def test_the_bot_registers_what_the_game_declared_and_keeps_it_for_a_start_without_the_game(
        link, monkeypatch):
    """Declared once, kept in bot_state: a bot starting while the game is down still knows
    !roll and answers «unavailable» instead of nothing."""
    from unittest.mock import AsyncMock

    from src.core.tasks import BackgroundTasks
    from src.twitch.roll.adapter import RollAdapter
    bot, client, _ = link
    bot.add_component = AsyncMock()
    adapter = RollAdapter(bot, FollowerCache(), BackgroundTasks(), lambda: False)
    adapter.client = client
    chat = chat_component(bot, game=False)
    chat._roll = client
    await adapter.attach(chat)
    assert chat._registry.resolve('!roll') is None
    assert await adapter._refresh_commands()
    assert chat._registry.resolve('!roll 5').trigger == '!roll'
    assert chat._registry.resolve('!rollboard').public

    later = chat_component(bot, game=False)
    later._roll = client
    await RollAdapter(bot, FollowerCache(), BackgroundTasks(), lambda: False).attach(later)
    assert later._registry.resolve('!rollstat').trigger == '!rollstat'
    await adapter.client.close()


def test_a_new_declaration_replaces_the_old_one(db):
    from src.twitch.roll.client import GameCommand
    chat = chat_component(FakeBot())
    chat.set_game_commands([GameCommand('!dice')])
    assert chat._registry.resolve('!roll') is None
    assert chat._registry.resolve('!dice').trigger == '!dice'


def test_a_queued_event_never_undoes_a_fresher_state():
    """Chat events wait in the bot's queue with the state of their moment: one that arrives
    after a later direct call must not take the game back off the air."""
    remote = RemoteBot(ServiceClient('бот', 'http://bot.test'))
    remote.update({'session_id': '2026-11-22 19:46', 'live': True, 'rewards_active': True, 'at': 200.0})
    remote.update({'session_id': '2026-11-22', 'live': False, 'rewards_active': False, 'at': 100.0})
    assert remote.stream_live and remote.session_id == '2026-11-22 19:46'


async def test_a_game_command_runs_the_chat_hook_first(link, monkeypatch):
    """The command may overtake its own chat event: a first «!roll» must still start the
    perk out loud, so the game runs the hook before the handler."""
    from src.roll import service as service_module
    bot, client, _ = link
    order = []

    async def hook(*args):
        order.append('chat')

    async def handler(ctx):
        order.append('command')
    monkeypatch.setattr(service_module.perks, 'on_chat', hook)
    monkeypatch.setattr(service_module.tiers, 'on_chat', hook)
    monkeypatch.setitem(service_module.HANDLERS, '!roll', handler)
    await client.command('!roll', _ctx(bot, '!roll'))
    assert order == ['chat', 'chat', 'command']


async def test_a_limit_refusal_from_the_game_shares_the_bots_brake(link):
    """The game's refusal goes through the bot's own once-per-30-s brake: a viewer refused by
    a bot command a moment ago does not hear the game refuse them too."""
    bot, client, remote = link
    ctx = _ctx(bot, '!roll')
    with client._api.hold(ctx.message):
        await remote.reply(ctx.message.id, 'отказ 1', deny=True)
        await remote.reply(ctx.message.id, 'отказ 2', deny=True)
    ctx.message.respond.assert_awaited_once_with('отказ 1')
    assert bot.cooldown_remaining('gop', 'deny')


async def test_a_stream_event_tells_the_game_which_stream_it_was(link, monkeypatch):
    """On an end the state already holds the date session: the event names the stream that ended."""
    from src.roll import service as service_module
    bot, client, remote = link
    told = []

    async def on_stream(bot_, session_id, *, live):
        told.append((session_id, live))
    monkeypatch.setattr(service_module, 'on_stream', on_stream)
    bot.stream_live = False
    task = asyncio.create_task(client.events_loop())
    try:
        await client.stream('2026-11-22 19:46')
        await asyncio.wait_for(client._events.join(), 3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert told == [('2026-11-22 19:46', False)]
    assert not remote.stream_live


async def test_the_game_asks_the_bot_who_follows_and_who_is_in_chat(link):
    bot, _, remote = link
    bot.follows = False
    assert await remote.follows('42') is False
    bot.fetch_chatters.return_value = {'gop', 'lurker'}
    assert await remote.fetch_chatters() == {'gop', 'lurker'}
    bot.fetch_chatters.return_value = None
    assert await remote.fetch_chatters() is None


async def test_a_reply_after_the_command_ended_is_not_sent(link):
    """The bot keeps the message only while the command's call runs."""
    _, _, remote = link
    assert await remote.reply('gone', 'поздно') is False


async def test_without_the_bot_the_game_loses_its_lines_and_lets_the_buyer_play(caplog):
    """The bot is down: lines are logged as lost, the follow check fails open, a lost bot is
    logged once however many checks fail."""
    caplog.set_level(logging.INFO)
    remote = RemoteBot(ServiceClient('бот', 'http://127.0.0.1:9'))
    try:
        assert await remote.sync() is False
        assert await remote.sync() is False
        assert caplog.text.count('Бот не ответил') == 1
        assert await remote.send_chat_message('строка') is False
        assert await remote.reply('m-1', 'ответ') is False
        assert await remote.fetch_chatters() is None
        assert await remote.follows('42') is True
    finally:
        await remote._client.close()


async def _flaky_game(failures: int) -> tuple[TestServer, list[str]]:
    """A game that answers 500 to its first `failures` events, then 200."""
    from aiohttp import web
    hits: list[str] = []

    async def handler(request):
        hits.append(request.path)
        return web.json_response({}, status=500 if len(hits) <= failures else 200)
    app = web.Application()
    app.router.add_post('/events/{kind}', handler)
    server = TestServer(app)
    await server.start_server()
    return server, hits


@pytest.mark.parametrize(('failures', 'hits_expected', 'lost'), [
    (1, 2, False),      # a restart: the stream event goes through on the next try
    (99, 3, True),      # the game stays down: said once all the tries are spent
])
async def test_a_stream_event_is_retried_while_the_game_restarts(db, monkeypatch, caplog, failures, hits_expected,
                                                                 lost):
    from src.twitch.roll import client as client_module
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(client_module, 'STREAM_RETRIES', 3)
    monkeypatch.setattr(client_module, 'STREAM_RETRY_SECONDS', 0)
    server, hits = await _flaky_game(failures)
    monkeypatch.setattr(Service, 'ROLL_URL', str(server.make_url('')).rstrip('/'))
    bot = FakeBot()
    client = RollClient(bot, BotApi(bot, FollowerCache()))
    try:
        await client._send('/events/stream', {'state': client._state(), 'session_id': 's-1'})
        assert len(hits) == hits_expected
        assert ('так и не узнала' in caplog.text) is lost
        assert ('снова на связи' in caplog.text) is not lost
    finally:
        await client.close()
        await server.close()


async def test_a_chat_event_is_tried_once(db, monkeypatch):
    """A lost chat event costs a perk at most; retrying would hold up every later one."""
    server, hits = await _flaky_game(99)
    monkeypatch.setattr(Service, 'ROLL_URL', str(server.make_url('')).rstrip('/'))
    bot = FakeBot()
    client = RollClient(bot, BotApi(bot, FollowerCache()))
    try:
        await client._send('/events/chat', {'state': client._state(), 'user': 'gop', 'tier': 'regular'})
        assert hits == ['/events/chat']
    finally:
        await client.close()
        await server.close()


async def test_the_game_down_at_a_refresh_keeps_the_commands_it_declared(link, monkeypatch):
    """A refresh while the game restarts changes nothing; an unchanged list is not registered again."""
    from unittest.mock import AsyncMock

    from src.core.tasks import BackgroundTasks
    from src.twitch.roll.adapter import RollAdapter
    bot, client, _ = link
    bot.add_component = AsyncMock()
    adapter = RollAdapter(bot, FollowerCache(), BackgroundTasks(), lambda: False)
    adapter.client = client
    chat = chat_component(bot, game=False)
    chat._roll = client
    await adapter.attach(chat)
    assert await adapter._refresh_commands()
    registered = []
    monkeypatch.setattr(chat, 'set_game_commands', registered.append)
    assert await adapter._refresh_commands()
    assert registered == []
    monkeypatch.setattr(client, 'commands', AsyncMock(side_effect=RollUnavailable('down')))
    assert await adapter._refresh_commands() is False
    assert chat._registry.resolve('!roll 5').trigger == '!roll'
