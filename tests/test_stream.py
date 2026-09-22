"""The stream tracker: the session is the stream."""
import asyncio
import time

import pytest

from src.core import stream
from src.core.database import get_stream
from src.core.stream import StreamTracker
from src.gemini.memory import storage


async def test_offline_right_after_online_wins(db):
    """online() awaits the database several times before it records the stream; an
    offline arriving meanwhile must not be overwritten by it."""
    tracker = StreamTracker()
    await asyncio.gather(tracker.online('s1', time.time() - 60), tracker.offline())
    assert not tracker.live
    assert (await get_stream('s1')).ended_at is not None


async def test_restart_mid_stream_keeps_the_session(db):
    started = time.time() - 3600
    first = StreamTracker()
    assert await first.online('s1', started)
    session = first.session_id
    again = StreamTracker()
    assert not await again.online('s1', started)
    assert again.session_id == session


async def test_short_outage_keeps_the_session(db):
    tracker = StreamTracker()
    await tracker.online('s1', time.time() - 3600)
    session = tracker.session_id
    await tracker.offline()
    assert not await tracker.online('s2', time.time())
    assert tracker.session_id == session


@pytest.fixture
def process_in_utc(monkeypatch):
    """The process's own zone is UTC, as in a container without TZ."""
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_session_and_memory_key_ignore_the_process_zone(process_in_utc):
    """The container, the host CLI and make oauth must name one moment alike: the
    memory's crash-rerun check compares these keys."""
    moment = 1789923600.0  # 2026-09-20 17:00 UTC
    assert stream._session_name(moment) == '2026-09-20 20:00'
    assert storage._key(moment) == '2026-09-20 20:00'
