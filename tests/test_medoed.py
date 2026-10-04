"""The medoed overlay: distinct chatters over five minutes decide the pose, the feed tells
the OBS page."""
import asyncio
import json
import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot as bot_module
from fakes import FakeBot, make_chatter, make_message
from src.core.component import ChatComponent
from src.core.config import Medoed
from src.local.medoed import feed, mood
from src.local.medoed.mood import MoodTracker, target


def _chat(tracker: MoodTracker, count: int, at: float, prefix: str = 'viewer') -> None:
    for i in range(count):
        tracker.saw(f'{prefix}{i}', now=at)


@pytest.mark.parametrize('chatters, expected', [
    (0, 'sad'), (2, 'sad'), (3, 'bored'), (5, 'bored'), (6, 'idle'), (9, 'idle'), (10, 'dance'), (40, 'dance'),
])
def test_the_pose_follows_the_number_of_chatters(chatters, expected):
    assert target(chatters)[0] == expected


@pytest.mark.parametrize('chatters, rate', [(9, 1.0), (10, 1.0), (11, 1.1), (13, 1.3), (15, 1.5), (30, 1.5)])
def test_the_dance_speeds_up_by_a_tenth_per_chatter_up_to_the_cap(chatters, rate):
    assert target(chatters)[1] == rate


def test_the_medoed_lies_until_someone_writes():
    tracker = MoodTracker()
    assert tracker.update(now=0) is False
    assert tracker.mood == 'sad'


def test_going_up_is_immediate_even_across_levels():
    """The overlay walks the poses one by one itself; the mood only names the goal."""
    tracker = MoodTracker()
    _chat(tracker, 10, at=0)
    assert tracker.update(now=0) is True
    assert tracker.mood == 'dance'


def test_one_chatter_writing_many_times_counts_once():
    tracker = MoodTracker()
    for _ in range(20):
        tracker.saw('Spammer', now=0)
    tracker.saw('spammer', now=1)
    tracker.update(now=1)
    assert tracker.chatters == 1
    assert tracker.mood == 'sad'


def test_writing_once_in_five_minutes_keeps_a_chatter_counted():
    tracker = MoodTracker()
    _chat(tracker, 3, at=0)
    tracker.update(now=0)
    _chat(tracker, 3, at=299)
    tracker.update(now=500)
    assert tracker.chatters == 3
    assert tracker.mood == 'bored'


def test_an_emptied_chat_walks_the_medoed_down_one_pose_a_minute():
    tracker = MoodTracker()
    _chat(tracker, 10, at=0)
    tracker.update(now=0)
    # everyone leaves the window at 300 s; then a step every STEP_DOWN_SECONDS
    steps = {}
    for t in range(0, 500, 5):
        tracker.update(now=t)
        steps.setdefault(tracker.mood, t)
    assert steps == {'dance': 0, 'idle': 360, 'bored': 420, 'sad': 480}


def test_a_partial_drop_stops_at_the_pose_the_chat_still_asks_for():
    """Three regulars keep writing after a crowd leaves: the medoed walks down to sitting
    and stays there."""
    tracker = MoodTracker()
    _chat(tracker, 10, at=0)
    tracker.update(now=0)
    for t in range(0, 1000, 5):
        _chat(tracker, 3, at=t)
        tracker.update(now=t)
    assert tracker.mood == 'bored'
    assert tracker.chatters == 3


def test_a_new_chatter_during_the_walk_down_lifts_the_mood_at_once():
    tracker = MoodTracker()
    _chat(tracker, 10, at=0)
    tracker.update(now=0)
    for t in range(300, 430, 5):
        tracker.update(now=t)
    assert tracker.mood == 'bored'
    _chat(tracker, 6, at=430, prefix='back')
    assert tracker.update(now=430) is True
    assert tracker.mood == 'idle'


def test_the_dance_slows_to_normal_while_it_waits_to_step_down():
    tracker = MoodTracker()
    _chat(tracker, 15, at=0)
    tracker.update(now=0)
    assert (tracker.mood, tracker.rate) == ('dance', 1.5)
    tracker.update(now=301)
    assert (tracker.mood, tracker.rate) == ('dance', 1.0)


def test_a_new_speed_counts_as_a_change():
    tracker = MoodTracker()
    _chat(tracker, 10, at=0)
    tracker.update(now=0)
    tracker.saw('one_more', now=1)
    assert tracker.update(now=1) is True
    assert tracker.rate == 1.1


def test_chat_is_not_counted_while_the_overlay_is_off(monkeypatch):
    monkeypatch.setattr(Medoed, 'ENABLED', False)
    feed.on_chat('gop')
    assert mood.tracker.update() is False
    assert mood.tracker.chatters == 0


def test_a_change_goes_to_every_listener(monkeypatch):
    monkeypatch.setattr(Medoed, 'ENABLED', True)
    queue = asyncio.Queue(feed.QUEUE_SIZE)
    feed._clients.add(queue)
    for nick in ('a', 'b', 'c'):
        feed.on_chat(nick)
    assert json.loads(queue.get_nowait()) == {'mood': 'bored', 'rate': 1.0, 'chatters': 3}
    assert queue.empty()


async def test_the_feed_sends_the_current_mood_on_connect_and_every_change(monkeypatch):
    monkeypatch.setattr(Medoed, 'ENABLED', True)
    async with TestClient(TestServer(feed.make_app())) as client:
        response = await client.get('/medoed/events')
        assert response.headers['Content-Type'] == 'text/event-stream'
        # the overlay is a local file in OBS: a cross-origin listener
        assert response.headers['Access-Control-Allow-Origin'] == '*'
        assert await _event(response) == {'mood': 'sad', 'rate': 1.0, 'chatters': 0}
        for nick in ('a', 'b', 'c', 'd', 'e', 'f'):
            feed.on_chat(nick)
        assert await _event(response) == {'mood': 'bored', 'rate': 1.0, 'chatters': 3}
        assert await _event(response) == {'mood': 'idle', 'rate': 1.0, 'chatters': 6}
        response.close()


async def test_the_state_endpoint_answers_once_with_json(monkeypatch):
    monkeypatch.setattr(Medoed, 'ENABLED', True)
    async with TestClient(TestServer(feed.make_app())) as client:
        response = await client.get('/medoed/state')
        assert await response.json() == {'mood': 'sad', 'rate': 1.0, 'chatters': 0}


async def _event(response) -> dict:
    lines = []
    while True:
        line = (await asyncio.wait_for(response.content.readline(), 2)).decode().strip()
        if not line:
            if lines:
                break
            continue
        lines.append(line)
    assert lines[0] == 'event: mood'
    return json.loads(lines[1].removeprefix('data: '))


def test_the_window_is_strict_a_chatter_leaves_exactly_at_its_end():
    tracker = MoodTracker()
    _chat(tracker, 3, at=0)
    tracker.update(now=299.999)
    assert tracker.chatters == 3
    tracker.update(now=300)
    assert tracker.chatters == 0


def test_a_level_that_comes_back_restarts_the_wait():
    """The chat dips below the current level and recovers: the next dip waits a full step."""
    tracker = MoodTracker()
    _chat(tracker, 6, at=0)
    tracker.update(now=0)
    _chat(tracker, 3, at=250)            # three of the six stay; the others leave at 300
    tracker.update(now=300)
    assert tracker.mood == 'idle'
    _chat(tracker, 3, at=330, prefix='back')
    tracker.update(now=330)              # six again: the wait from 300 is void
    tracker.update(now=365)
    assert tracker.mood == 'idle'
    # the three regulars leave at 550, the returners at 630: three stay until then
    tracker.update(now=550)
    tracker.update(now=609)
    assert tracker.mood == 'idle'
    tracker.update(now=610)
    assert tracker.mood == 'bored'


def test_a_new_target_during_the_wait_does_not_restart_it():
    """The chat drops in two steps 10 s apart: the first step down still comes 60 s after
    the first drop, the walk does not start over."""
    tracker = MoodTracker()
    _chat(tracker, 10, at=0)
    _chat(tracker, 4, at=10)             # four of the ten write again
    tracker.update(now=0)
    tracker.update(now=300)              # six leave: four are left, sitting is asked for
    tracker.update(now=310)              # the four leave: lying is asked for
    tracker.update(now=359)
    assert tracker.mood == 'dance'
    tracker.update(now=360)
    assert tracker.mood == 'idle'


def test_every_listener_gets_the_change(monkeypatch):
    monkeypatch.setattr(Medoed, 'ENABLED', True)
    first, second = asyncio.Queue(feed.QUEUE_SIZE), asyncio.Queue(feed.QUEUE_SIZE)
    feed._clients.update({first, second})
    for nick in ('a', 'b', 'c'):
        feed.on_chat(nick)
    assert json.loads(first.get_nowait())['mood'] == json.loads(second.get_nowait())['mood'] == 'bored'


def test_a_stuck_listener_keeps_only_the_newest_states():
    """A listener that stopped reading loses the oldest states and never blocks the rest."""
    queue = asyncio.Queue(feed.QUEUE_SIZE)
    feed._clients.add(queue)
    for i in range(feed.QUEUE_SIZE + 5):
        feed._broadcast(str(i))
    kept = [queue.get_nowait() for _ in range(queue.qsize())]
    assert kept == [str(i) for i in range(5, feed.QUEUE_SIZE + 5)]


async def test_a_silent_feed_sends_pings(monkeypatch):
    """The overlay reconnects after a long silence, so a quiet chat must not look dead."""
    monkeypatch.setattr(feed, 'PING_SECONDS', 0.05)
    async with TestClient(TestServer(feed.make_app())) as client:
        response = await client.get('/medoed/events')
        await _event(response)
        assert (await _raw_event(response))[0] == 'event: ping'
        response.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def test_the_bot_stops_at_once_with_an_overlay_connected(monkeypatch):
    """A stream never ends by itself: the server's shutdown would wait for it longer than
    docker gives the whole bot to stop, and the rewards would stay open."""
    monkeypatch.setattr(Medoed, 'HOST', '127.0.0.1')
    monkeypatch.setattr(Medoed, 'PORT', _free_port())
    task = asyncio.create_task(feed.medoed_loop())
    async with aiohttp.ClientSession() as session:
        for _ in range(50):
            try:
                response = await session.get(f'http://127.0.0.1:{Medoed.PORT}/medoed/events')
                break
            except aiohttp.ClientConnectionError:
                await asyncio.sleep(0.02)
        await _event(response)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert await asyncio.wait_for(response.content.read(), 3) == b''


async def test_a_busy_port_leaves_the_rest_of_the_bot_alone(monkeypatch, caplog):
    with socket.socket() as taken:
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        monkeypatch.setattr(Medoed, 'HOST', '127.0.0.1')
        monkeypatch.setattr(Medoed, 'PORT', taken.getsockname()[1])
        await asyncio.wait_for(feed.medoed_loop(), 3)
    assert 'не открылся' in caplog.text
    assert 'Traceback' not in caplog.text


async def test_the_bots_own_message_is_counted(monkeypatch):
    """Every chatter counts, the bot itself included: the filter that drops its own lines
    for everything else comes after the count."""
    monkeypatch.setattr(Medoed, 'ENABLED', True)
    bot = FakeBot()
    itself = make_chatter('securityexpert')
    itself.id = bot.bot_id
    await ChatComponent(bot).event_message(make_message('hi', itself))
    mood.tracker.update()
    assert mood.tracker.chatters == 1


async def test_a_repeated_delivery_counts_one_chatter(monkeypatch, db):
    monkeypatch.setattr(Medoed, 'ENABLED', True)
    component = ChatComponent(FakeBot())
    message = make_message('привет', make_chatter('gop'))
    await component.event_message(message)
    await component.event_message(message)
    mood.tracker.update()
    assert mood.tracker.chatters == 1


@pytest.mark.parametrize('enabled', [True, False])
async def test_the_feed_starts_only_when_enabled(monkeypatch, enabled):
    monkeypatch.setattr(Medoed, 'ENABLED', enabled)
    started = asyncio.Event()

    async def loop():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bot_module, 'medoed_loop', loop)
    bot = bot_module.Bot()
    bot._start_medoed()
    assert bot._tasks.running('medoed') is enabled
    await bot.stop_background_tasks()


async def _raw_event(response) -> list[str]:
    lines = []
    while True:
        line = (await asyncio.wait_for(response.content.readline(), 2)).decode().strip()
        if not line:
            if lines:
                return lines
            continue
        lines.append(line)
