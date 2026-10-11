"""The game on the stream: what the OBS page gets from !roll, the rewards and the perks."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeBot, make_chatter, make_message
from src.core.config import Rewards, Roll
from src.twitch.core import sse
from src.twitch.core.commands import CommandContext
from src.twitch.local.roll import game, overlay, rules
from src.twitch.local.roll.command import handle_roll, handle_rollstat
from src.twitch.local.roll.redemption import handle_redemption
from src.twitch.local.roll.storage import save_roll


@pytest.fixture
def page(monkeypatch):
    """The overlay switched on, with one open page: a queue of the JSON it was sent."""
    monkeypatch.setattr(Roll, 'OVERLAY_ENABLED', True)
    queue = asyncio.Queue(sse.QUEUE_SIZE)
    overlay.feed.clients.add(queue)
    return queue


def sent(queue: asyncio.Queue) -> list[dict]:
    return [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]


async def roll(bot: FakeBot, user: str) -> None:
    message = make_message('!roll', make_chatter(user))
    await handle_roll(CommandContext(message=message, user=user, prompt='!roll', original_text='!roll',
                                     session_id=bot.session_id, bot=bot))


def redeem(bot, action, redemption_id, user, user_input=''):
    async def follows() -> bool:
        return True
    return handle_redemption(bot, action, redemption_id, user, user_input, follows=follows)


async def test_a_roll_from_chat_reaches_the_page_with_the_titles(db, page, monkeypatch):
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: 42)
    bot = FakeBot()
    await roll(bot, 'gop')
    [event] = sent(page)
    assert event == {
        'type': 'roll', 'who': 'gop', 'actor': None, 'value': 42, 'old': None, 'min': Roll.MIN, 'max': Roll.MAX,
        'ceiling': None, 'next': None, 'hold': None, 'floor': event['floor'],
        'titles': {'best': None, 'worst': {'nick': 'gop', 'value': 42}},
    }


async def test_a_curse_carries_the_ceiling_and_the_next_one(db, page, monkeypatch):
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: b)
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 90, free_throw=True)
    await save_roll(bot.session_id, 'mate', 50, free_throw=True)
    assert await redeem(bot, game.Action.CURSE, 'r1', 'foe', 'gop') is True
    [event] = sent(page)
    assert (event['type'], event['who'], event['actor'], event['old']) == ('curse', 'gop', 'foe', 90)
    assert event['value'] == event['ceiling'] == Rewards.CURSE_CEILING
    assert event['next'] == Rewards.CURSE_CEILING - Rewards.CURSE_STEP
    assert event['titles'] == {
        'best': {'nick': 'gop', 'value': Rewards.CURSE_CEILING}, 'worst': {'nick': 'mate', 'value': 50},
    }


async def test_a_reroll_into_a_shield_bounces_and_a_plain_refusal_only_wakes_the_titles(db, page):
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 90, free_throw=True)
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop') is True
    assert await redeem(bot, game.Action.REROLL, 'r2', 'foe', 'gop') is False
    assert await redeem(bot, game.Action.REROLL, 'r3', 'foe', 'foe') is False
    shield, blocked, refused = sent(page)
    assert shield == {'type': 'shield', 'who': 'gop', 'minutes': Rewards.SHIELD_MINUTES}
    assert (blocked['type'], blocked['who'], blocked['actor'], blocked['by']) == ('blocked', 'gop', 'foe', 'shield')
    assert refused == {'type': 'wake'}


async def test_a_command_without_a_card_wakes_the_titles(db, page, monkeypatch):
    """!rollstat, a !roll with no throws left and a malformed !roll show no card, but the
    plate comes back for its hold. Offline nothing is sent: the plate is hidden anyway."""
    bot = FakeBot()
    message = make_message('!rollstat', make_chatter('gop'))
    await handle_rollstat(CommandContext(message=message, user='gop', prompt='!rollstat', original_text='!rollstat',
                                         session_id=bot.session_id, bot=bot))
    assert sent(page) == [{'type': 'wake'}]
    monkeypatch.setattr(game, 'free_throw', AsyncMock(return_value=game.Outcome(game.Status.NO_FREE_LEFT)))
    await roll(bot, 'gop')
    assert sent(page) == [{'type': 'wake'}]
    message = make_message('!roll 5', make_chatter('gop'))
    await handle_roll(CommandContext(message=message, user='gop', prompt='!roll 5', original_text='!roll 5',
                                     session_id=bot.session_id, bot=bot, args='5'))
    assert sent(page) == [{'type': 'wake'}]
    bot.stream_live = False
    await roll(bot, 'gop')
    assert sent(page) == []


async def test_a_cleanse_that_returns_the_roll_shows_both_numbers(db, page, monkeypatch):
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: a)
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 90, free_throw=True)
    await redeem(bot, game.Action.CURSE, 'r1', 'foe', 'gop')
    await redeem(bot, game.Action.CLEANSE, 'r2', 'gop', 'gop')
    _, cleanse = sent(page)
    assert (cleanse['type'], cleanse['value'], cleanse['old']) == ('cleanse', 90, 1)
    assert cleanse['titles']['worst'] == {'nick': 'gop', 'value': 90}


def test_the_perks_name_the_blessing_and_the_evil_eye():
    assert overlay.perk_on('gop', game.Perk.SHIELD)['perk'] == 'bless'
    assert overlay.perk_on('gop', game.Perk.CURSE)['perk'] == 'sglaz'
    assert overlay.perks('best', None)['worst'] is None


async def test_nothing_is_sent_while_the_overlay_is_off(db, monkeypatch):
    monkeypatch.setattr(Roll, 'OVERLAY_ENABLED', False)
    queue = asyncio.Queue(sse.QUEUE_SIZE)
    overlay.feed.clients.add(queue)
    await roll(FakeBot(), 'gop')
    assert queue.empty()
    assert overlay._state['titles'] == {'best': None, 'worst': None}


async def test_a_broken_page_feed_never_reaches_the_game(db, page, monkeypatch, caplog):
    def broken(_data):
        raise RuntimeError('boom')
    monkeypatch.setattr(overlay.feed, 'publish', broken)
    message = make_message('!roll', make_chatter('gop'))
    await handle_roll(CommandContext(message=message, user='gop', prompt='!roll', original_text='!roll',
                                     session_id=FakeBot().session_id, bot=FakeBot()))
    message.respond.assert_awaited_once()
    assert 'не отправлено' in caplog.text


def _app() -> web.Application:
    app = web.Application()
    overlay.add_routes(app)
    return app


async def test_a_new_page_starts_from_the_titles_in_the_database(db, page):
    """A reloaded Browser Source shows the titles at once, before any new event."""
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 10, free_throw=True)
    await save_roll(bot.session_id, 'king', 99, free_throw=True)
    await overlay.refresh(bot.session_id, live=True)
    async with TestClient(TestServer(_app())) as client:
        response = await client.get('/roll/events')
        assert response.headers['Content-Type'] == 'text/event-stream'
        lines = []
        while len(lines) < 2:
            line = (await asyncio.wait_for(response.content.readline(), 2)).decode().strip()
            if line:
                lines.append(line)
        assert lines[0] == 'event: roll'
        state = json.loads(lines[1].removeprefix('data: '))
        # A refresh during a stream counts as a move: a restarted bot shows the titles at once
        assert state == {'type': 'state', 'live': True, 'min': Roll.MIN, 'max': Roll.MAX, 'quiet': 0, 'titles': {
            'best': {'nick': 'king', 'value': 99}, 'worst': {'nick': 'gop', 'value': 10}}}
        response.close()


async def test_titles_from_an_event_become_the_state_of_the_next_page(db, page, monkeypatch):
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: 7)
    await roll(FakeBot(), 'gop')
    async with TestClient(TestServer(_app())) as client:
        state = await (await client.get('/roll/state')).json()
    assert state['titles']['worst'] == {'nick': 'gop', 'value': 7}
    # The page reloaded right after a move keeps the rest of the titles' hold
    assert state['quiet'] == 0


async def test_the_bot_serves_the_page_itself(page):
    async with TestClient(TestServer(_app())) as client:
        response = await client.get('/roll/')
        assert response.content_type == 'text/html'
        assert '/roll/events' in await response.text()
        response = await client.get('/roll', allow_redirects=False)
        assert response.status == 302
        assert response.headers['Location'] == '/roll/'
        # The page's parameters survive the redirect
        response = await client.get('/roll?layout=hud&scale=0.8', allow_redirects=False)
        assert response.headers['Location'] == '/roll/?layout=hud&scale=0.8'


async def test_a_refresh_keeps_titles_a_throw_published_meanwhile(db, page, monkeypatch):
    """refresh() reads the DB across an await: a throw in between must not be overwritten."""
    fresh = {'best': None, 'worst': {'nick': 'gop', 'value': 7}}

    async def titles(_session_id):
        overlay.publish({'type': 'roll', 'titles': fresh})
        return game.Standings(None, None)
    monkeypatch.setattr(game, 'titles', titles)
    await overlay.refresh('s', live=True)
    assert overlay._state['titles'] == fresh
