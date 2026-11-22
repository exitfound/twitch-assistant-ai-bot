"""The chat bridge Twitch → cinema hall: what goes, what stays, and when."""
import asyncio
import logging

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core import content
from src.core.config import Film
from src.twitch.core.component import ChatComponent
from src.twitch.local import film, film_bridge


@pytest.fixture
def bridge(monkeypatch):
    """The bridge switched on, a session open, the gate replaced: `bridge.posted` – what reached it."""
    monkeypatch.setattr(Film, 'URL', 'https://kino.example')
    monkeypatch.setattr(Film, 'API_TOKEN', 'token')
    monkeypatch.setattr(Film, 'ENABLED', True)
    monkeypatch.setattr(Film, 'BRIDGE', True)
    monkeypatch.setattr(Film, 'HALL_MARK', '🎬 [KZ]')
    monkeypatch.setattr(film, '_open', True)
    monkeypatch.setattr(film_bridge, '_outbox', None)
    state = type('Hall', (), {})()
    state.posted, state.error = [], None

    async def request(method, path, body=None, limit=None):
        assert (method, path) == ('POST', film.CHAT_PATH)
        if state.error:
            raise state.error
        state.posted.append(body['body'])
        return {'sent': True}
    monkeypatch.setattr(film, 'request', request)
    return state


def queued_lines() -> list[dict]:
    q = film_bridge._queue()
    return [q.get_nowait() for _ in range(q.qsize())]


def queued() -> list[str]:
    return [line['body'] for line in queued_lines()]


# --- Twitch → hall ----------------------------------------------------------------

def test_a_viewer_line_is_queued_for_the_hall(bridge):
    film_bridge.from_twitch('Gopnik', '  всем   привет ')
    assert queued() == ['texts.film_bridge_to_hall']


@pytest.mark.parametrize('text', ['!roll', '!film', '   '])
def test_commands_and_empty_lines_stay_in_twitch(bridge, text):
    film_bridge.from_twitch('Gopnik', text)
    assert queued() == []


def test_a_line_that_came_from_the_hall_does_not_go_back(bridge):
    film_bridge.from_twitch('Gopnik', '🎬 [KZ] привет из кинозала')
    assert queued() == []


def test_without_a_mark_nothing_is_left_out(bridge, monkeypatch):
    monkeypatch.setattr(Film, 'HALL_MARK', '')
    film_bridge.from_twitch('Gopnik', '🎬 [KZ] попкорн')
    assert queued() == ['texts.film_bridge_to_hall']


def test_nothing_goes_while_the_hall_is_closed(bridge, monkeypatch):
    monkeypatch.setattr(film, '_open', False)
    film_bridge.from_twitch('Gopnik', 'привет')
    assert queued() == []


def test_nothing_goes_with_the_bridge_off(bridge, monkeypatch):
    monkeypatch.setattr(Film, 'BRIDGE', False)
    film_bridge.from_twitch('Gopnik', 'привет')
    assert queued() == []


def test_a_full_queue_drops_the_line(bridge, caplog):
    for _ in range(film_bridge.OUTBOX_MAX):
        film_bridge.from_twitch('Gopnik', 'привет')
    with caplog.at_level(logging.WARNING):
        film_bridge.from_twitch('Gopnik', 'лишнее')
    assert film_bridge._queue().qsize() == film_bridge.OUTBOX_MAX
    assert 'очередь в кинозал полна' in caplog.text


async def test_the_sender_posts_in_order_and_warns_once(bridge, caplog):
    film_bridge.from_twitch('a', 'раз')
    film_bridge.from_twitch('b', 'два')
    bridge.error = film.GateError('down')
    film_bridge.from_twitch('c', 'три')
    sender = asyncio.create_task(film_bridge.bridge_loop())
    await asyncio.sleep(0.01)
    sender.cancel()
    assert bridge.posted == []
    bridge.error = None
    with caplog.at_level(logging.WARNING):
        sender = asyncio.create_task(film_bridge.bridge_loop())
        film_bridge.from_twitch('a', 'раз')
        film_bridge.from_twitch('b', 'два')
        await asyncio.sleep(0.01)
        sender.cancel()
    assert bridge.posted == ['texts.film_bridge_to_hall'] * 2


async def test_the_bot_s_own_line_goes_to_the_hall_once(db, bridge):
    """Its answers, announcements and remarks: the hall sees what Twitch sees."""
    bot = FakeBot()
    component = ChatComponent(bot)
    own = make_chatter('securityexpert')
    own.id = str(bot.bot_id)
    message = make_message('@gopnik держи ответ', own)
    await component.event_message(message)
    await component.event_message(message)  # redelivered
    assert queued() == ['texts.film_bridge_to_hall']


async def test_a_hall_line_is_read_without_its_mark_and_stays_out_of_the_hall(db, bridge):
    from src.core.db import chat as db_chat
    component = ChatComponent(FakeBot())
    await component.event_message(make_message('🎬 [KZ] кино огонь', make_chatter('fan')))
    assert queued() == []
    rows = await db_chat.get_recent_chat('2026-09-22 20:00', 5)
    assert any('кино огонь' in str(r) and '🎬 [KZ]' not in str(r) for r in rows), rows


async def test_a_command_from_the_hall_works_like_in_twitch(db, bridge, monkeypatch):
    monkeypatch.setattr(Film, 'INFO_ACCESS', frozenset({'all'}))

    async def gate(method, title=None, limit=None):
        return {'active': True, 'code': '4817', 'title': None}
    monkeypatch.setattr(film, 'gate', gate)
    component = ChatComponent(FakeBot())
    message = make_message('🎬 [KZ] !film', make_chatter('fan'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.film_info')


def test_from_hall_takes_only_a_leading_mark(bridge, monkeypatch):
    assert film_bridge.from_hall('🎬 [KZ]  !roll') == '!roll'
    assert film_bridge.from_hall('привет 🎬 [KZ]') is None
    monkeypatch.setattr(Film, 'BRIDGE', False)  # the gate posts hall lines without the bot's bridge
    assert film_bridge.from_hall('🎬 [KZ] !film') == '!film'
    monkeypatch.setattr(Film, 'ENABLED', False)
    assert film_bridge.from_hall('🎬 [KZ] привет') is None


async def test_a_viewer_line_reaches_the_queue_through_the_dispatcher(db, bridge):
    component = ChatComponent(FakeBot())
    await component.event_message(make_message('кино огонь', make_chatter('fan')))
    assert queued() == ['texts.film_bridge_to_hall']


# --- the open flag and the real texts ------------------------------------------------

async def test_the_session_answers_set_the_open_flag(monkeypatch):
    answers = iter([{'active': True, 'code': '1'}, {'active': False, 'closed': True}, {'active': False}])

    async def request(method, path, body=None, limit=None):
        return next(answers)
    monkeypatch.setattr(film, 'request', request)
    monkeypatch.setattr(film, '_open', False)
    await film.gate('POST')
    assert film.is_open()
    await film.gate('DELETE')
    assert not film.is_open()
    await film.gate('GET')
    assert not film.is_open()


def test_the_real_texts_read_as_expected(monkeypatch):
    from pathlib import Path
    real = Path(__file__).parents[1] / 'docs' / 'CONTENT.md'
    monkeypatch.setattr(content, 'CONTENT_PATH', real)
    monkeypatch.setattr(content, '_content', content._ContentFile(real))
    assert content.Content.text('film_bridge_to_hall', user='Gopnik', text='привет') == 'Gopnik: привет'


# --- the look: the author's colour and the line's emotes go along -----------------------

def _twitch_message(text, color=None, emotes=(), badges=()):
    from types import SimpleNamespace
    message = make_message(text, make_chatter('fan'))
    message.badges = [SimpleNamespace(set_id=s, id=v, info='') for s, v in badges]
    message.colour = SimpleNamespace(html=color) if color else None
    message.fragments = [SimpleNamespace(type='text', text='кино ', emote=None)] + [
        SimpleNamespace(type='emote', text=name, emote=SimpleNamespace(id=emote_id)) for name, emote_id in emotes]
    return message


def test_look_takes_the_colour_and_the_emotes():
    message = _twitch_message('кино Kappa exitfoLove', '#ff0000', [('Kappa', '25'), ('exitfoLove', 'emotesv2_ab')],
                              [('moderator', '1'), ('subscriber', '12')])
    assert film_bridge.look(message) == ('#ff0000', {'Kappa': '25', 'exitfoLove': 'emotesv2_ab'},
                                         ['moderator/1', 'subscriber/12'])


def test_look_of_a_plain_message_is_empty():
    assert film_bridge.look(make_message('привет')) == (None, {}, [])


async def test_the_colour_and_the_emotes_reach_the_gate_with_the_line(db, bridge):
    component = ChatComponent(FakeBot())
    await component.event_message(_twitch_message('кино Kappa', '#00ff7f', [('Kappa', '25')], [('vip', '1')]))
    assert queued_lines() == [{'body': 'texts.film_bridge_to_hall', 'color': '#00ff7f', 'emotes': {'Kappa': '25'},
                               'badges': ['vip/1']}]


async def test_a_line_without_a_colour_or_emotes_sends_only_the_text(db, bridge):
    component = ChatComponent(FakeBot())
    await component.event_message(_twitch_message('кино'))
    assert queued_lines() == [{'body': 'texts.film_bridge_to_hall', 'badges': []}]
