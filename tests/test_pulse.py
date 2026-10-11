"""The pulse: what the chat is about, retold – built from the chat, cached per session."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.core.config import Pulse
from src.core.database import save_chat_message
from src.gemini import pulse


async def _chat(session_id: str, count: int) -> None:
    for i in range(count):
        await save_chat_message(session_id, 'gop', f'сообщение {i}')


async def test_too_little_chat_has_no_pulse(db, monkeypatch):
    generate = AsyncMock(return_value='пульс')
    monkeypatch.setattr(pulse, 'generate', generate)
    await _chat('s', pulse.MIN_MESSAGES - 1)
    assert await pulse.build('s') is None
    generate.assert_not_awaited()


async def test_pulse_is_cleaned_line_by_line(db, monkeypatch):
    """Lines stay lines – each is one thought; list markers and asterisks go."""
    monkeypatch.setattr(pulse, 'generate', AsyncMock(return_value='- **чат** про варик\n\n* gop ноет \u2014 опять'))
    await _chat('s', pulse.MIN_MESSAGES)
    assert await pulse.build('s') == 'чат про варик\ngop ноет – опять'


async def test_the_cached_pulse_is_of_this_session_and_fresh(db, monkeypatch):
    monkeypatch.setattr(pulse, 'generate', AsyncMock(return_value='чат про варик'))
    await _chat('s', pulse.MIN_MESSAGES)
    await pulse.refresh('s')
    assert pulse.current('s') == 'чат про варик'
    assert pulse.current('other') is None
    session, built_at, text = pulse._pulse
    monkeypatch.setattr(pulse, '_pulse', (session, built_at - 2 * Pulse.INTERVAL_MINUTES * 60 - 1, text))
    assert pulse.current('s') is None


async def test_a_failed_refresh_keeps_the_previous_pulse(db, monkeypatch):
    await _chat('s', pulse.MIN_MESSAGES)
    monkeypatch.setattr(pulse, 'generate', AsyncMock(return_value='чат про варик'))
    await pulse.refresh('s')
    monkeypatch.setattr(pulse, 'generate', AsyncMock(return_value=None))
    await pulse.refresh('s')
    assert pulse.current('s') == 'чат про варик'


async def test_the_loop_rebuilds_only_after_new_messages(db, monkeypatch):
    """A quiet chat keeps its pulse at no cost."""
    from fakes import FakeBot
    from src.twitch.gemini import pulse as pulse_loop
    bot = FakeBot(session_id='s')
    refresh = AsyncMock()
    monkeypatch.setattr(pulse, 'refresh', refresh)
    rounds = []

    async def sleep(seconds):
        rounds.append(seconds)
        if len(rounds) == 1:
            await _chat('s', 1)
        if len(rounds) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(pulse_loop.asyncio, 'sleep', sleep)
    await _chat('s', 1)
    with pytest.raises(asyncio.CancelledError):
        await pulse_loop.pulse_loop(bot)
    # Round 1: a message – built; round 2: one more – built; round 3: nothing new
    assert refresh.await_count == 2


async def _rounds(monkeypatch, bot, refresh, rounds: int) -> None:
    """Run the loop for `rounds` rounds with no new chat after the first message."""
    from src.twitch.gemini import pulse as pulse_loop
    monkeypatch.setattr(pulse, 'refresh', refresh)
    done = []

    async def sleep(seconds):
        done.append(seconds)
        if len(done) == rounds:
            raise asyncio.CancelledError
    monkeypatch.setattr(pulse_loop.asyncio, 'sleep', sleep)
    await _chat('s', 1)
    with pytest.raises(asyncio.CancelledError):
        await pulse_loop.pulse_loop(bot)


async def test_a_failed_build_is_tried_again_without_a_new_message(db, monkeypatch):
    from fakes import FakeBot
    refresh = AsyncMock(side_effect=[False, True])
    await _rounds(monkeypatch, FakeBot(session_id='s'), refresh, 3)
    # Round 1: failed; round 2: tried again and built; round 3: nothing new
    assert refresh.await_count == 2


async def test_offline_chat_builds_no_pulse(db, monkeypatch):
    """Outside a stream the pulse would cost a call every interval while anyone writes."""
    from fakes import FakeBot
    refresh = AsyncMock(return_value=True)
    await _rounds(monkeypatch, FakeBot(session_id='s', stream_live=False), refresh, 2)
    refresh.assert_not_awaited()
