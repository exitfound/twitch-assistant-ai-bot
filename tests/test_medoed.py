"""The medoed overlay: distinct chatters over five minutes decide the pose, the feed tells
the OBS page."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

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
