"""The bot and the game as separate services: every call over real HTTP, both APIs served.

The bot's side is FakeBot behind its internal API (src/twitch/core/api.py); the game is
RemoteBot behind the game's API (src/roll/service.py); RollClient links them.
"""
import asyncio
import contextlib

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
    await adapter._load_commands()
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
