"""The stream tracker: the session is the stream."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.twitch.core import stream
from src.core.database import get_stream
from src.twitch.core.stream import StreamTracker
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


async def test_twitch_unreachable_at_startup_resumes_the_open_stream(db):
    """A restart mid-stream during a network outage must not split the session."""
    first = StreamTracker()
    await first.online('s1', time.time() - 3600)
    again = StreamTracker()
    assert await again.resume_open()
    assert again.live and again.stream_id == 's1' and again.session_id == first.session_id


async def test_nothing_to_resume_after_a_closed_stream(db):
    tracker = StreamTracker()
    await tracker.online('s1', time.time() - 3600)
    await tracker.offline()
    again = StreamTracker()
    assert not await again.resume_open()
    assert not again.live


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


# --- the watch against Twitch ---------------------------------------------------

LIVE = ('s1', 1000.0)


async def _watch(answers: list, stream_id: str | None, monkeypatch) -> SimpleNamespace:
    """Run watch_stream() over Twitch's scripted answers and return the fake bot.

    An answer that is an exception is raised as a failed request.
    """
    monkeypatch.setattr(stream, 'CHECK_SECONDS', 0)
    answers = list(answers)

    async def fetch():
        if not answers:
            raise asyncio.CancelledError     # the script is over: stop the loop
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    bot = SimpleNamespace(stream=SimpleNamespace(stream_id=stream_id), fetch_live_stream=fetch)

    async def offline():
        bot.stream.stream_id = None

    async def online(stream_id, started_at):
        bot.stream.stream_id = stream_id
    bot.stream_went_offline = AsyncMock(side_effect=offline)
    bot.stream_went_online = AsyncMock(side_effect=online)
    with pytest.raises(asyncio.CancelledError):
        await stream.watch_stream(bot)
    return bot


async def test_one_empty_answer_does_not_end_the_stream(monkeypatch):
    """Twitch's list lags at a stream's edges: one miss would end the session, close the
    game and pause the rewards in the middle of a stream."""
    misses = [None] * (stream.CONFIRMATIONS - 1)
    bot = await _watch([*misses, LIVE, *misses, LIVE], 's1', monkeypatch)
    bot.stream_went_offline.assert_not_awaited()


async def test_a_confirmed_end_closes_the_stream_once(monkeypatch):
    bot = await _watch([None] * (stream.CONFIRMATIONS * 3), 's1', monkeypatch)
    bot.stream_went_offline.assert_awaited_once()


async def test_answers_that_disagree_with_each_other_change_nothing(monkeypatch):
    bot = await _watch([None, ('s2', 2000.0)] * stream.CONFIRMATIONS, 's1', monkeypatch)
    bot.stream_went_offline.assert_not_awaited()
    bot.stream_went_online.assert_not_awaited()


async def test_a_missed_start_is_opened_once(monkeypatch):
    bot = await _watch([LIVE] * (stream.CONFIRMATIONS * 3), None, monkeypatch)
    bot.stream_went_online.assert_awaited_once_with(*LIVE)


async def test_a_failed_request_is_not_an_answer(monkeypatch):
    bot = await _watch([RuntimeError('helix down')] * (stream.CONFIRMATIONS * 2), 's1', monkeypatch)
    bot.stream_went_offline.assert_not_awaited()
