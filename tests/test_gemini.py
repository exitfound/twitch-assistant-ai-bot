"""The Gemini layer without Gemini: the request wrapper, the fallback ladder, sending
answers to chat and the per-stream limits. Every call to the model is a stub."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import errors, types

from fakes import FakeBot, make_chatter, make_message
from src.core.commands import CommandContext, Kind
from src.core.config import Caps, Emote, Gemini, Who
from src.core.database import count_bot_uses, get_db
from src.gemini import client, commands, ladder, responder
from src.gemini.client import BLOCK_INPUT, BLOCK_OUTPUT, EMPTY, ERROR
from src.gemini.responder import CHUNK_SLACK, respond_and_save, send_chunked
from src.gemini.who import WHO_KIND

# --- generate_checked -------------------------------------------------------


def _response(text=None, *, blocked=False, finish=None, usage=None):
    return SimpleNamespace(
        text=text,
        prompt_feedback=SimpleNamespace(block_reason='OTHER') if blocked else None,
        candidates=[SimpleNamespace(finish_reason=finish)] if finish else [],
        usage_metadata=usage,
    )


@pytest.fixture
def gemini(monkeypatch):
    """A fake client whose generate_content returns or raises what the test queues."""
    call = AsyncMock()
    fake = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=call)))
    monkeypatch.setattr(client, 'get_client', lambda: fake)
    monkeypatch.setattr(client, 'RETRY_BASE_DELAY', 0)
    monkeypatch.setattr(client.random, 'uniform', lambda a, b: 0)
    monkeypatch.setattr(client, 'usage', {'prompt': 0, 'cached': 0, 'output': 0})
    return call


async def test_text_comes_back(gemini):
    gemini.return_value = _response('ответ')
    assert await client.generate_checked('q', types.GenerateContentConfig()) == ('ответ', None)


async def test_input_block(gemini):
    gemini.return_value = _response(blocked=True)
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, BLOCK_INPUT)


async def test_output_block(gemini):
    gemini.return_value = _response(finish=types.FinishReason.PROHIBITED_CONTENT)
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, BLOCK_OUTPUT)


async def test_empty_answer(gemini):
    gemini.return_value = _response(finish=types.FinishReason.RECITATION)
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, EMPTY)


async def test_server_error_is_retried(gemini):
    gemini.side_effect = [errors.ServerError(503, {'error': {'message': 'busy'}}), _response('ок')]
    assert await client.generate_checked('q', types.GenerateContentConfig()) == ('ок', None)
    assert gemini.await_count == 2


async def test_rate_limit_gives_up_after_the_retries(gemini):
    gemini.side_effect = errors.ClientError(429, {'error': {'message': 'quota'}})
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, None)
    assert gemini.await_count == Gemini.RETRIES + 1


async def test_timeout_is_not_retried(gemini):
    """The viewer is already waiting GEMINI_TIMEOUT seconds."""
    gemini.side_effect = asyncio.TimeoutError
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, None)
    assert gemini.await_count == 1


async def test_client_error_is_final_and_named(gemini):
    """A 4xx other than 408/429 repeats for the same prompt: callers must not take it
    for an outage and retry it forever."""
    gemini.side_effect = errors.ClientError(400, {'error': {'message': 'bad'}})
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, ERROR)
    assert gemini.await_count == 1


@pytest.mark.parametrize(('code', 'error'), [
    (400, {'message': 'API key not valid.', 'status': 'INVALID_ARGUMENT', 'details': [{'reason': 'API_KEY_INVALID'}]}),
    (400, {'message': 'User location is not supported', 'status': 'FAILED_PRECONDITION'}),
    (403, {'message': 'Permission denied', 'status': 'PERMISSION_DENIED'}),
    (404, {'message': 'models/x is not found', 'status': 'NOT_FOUND'}),
])
async def test_account_error_is_an_outage_not_a_bad_prompt(gemini, code, error):
    """A revoked key, a retired model or an unsupported region fail every prompt: taken
    for a bad prompt, the memory would mark each conversation failed for good."""
    gemini.side_effect = errors.ClientError(code, {'error': error})
    assert await client.generate_checked('q', types.GenerateContentConfig()) == (None, None)
    assert gemini.await_count == 1


async def test_usage_is_counted(gemini):
    usage = SimpleNamespace(prompt_token_count=100, cached_content_token_count=40,
                            candidates_token_count=20, thoughts_token_count=5)
    gemini.return_value = _response('ок', usage=usage)
    await client.generate_checked('q', types.GenerateContentConfig())
    assert client.usage == {'prompt': 100, 'cached': 40, 'output': 25}


def test_config_carries_the_thinking_budget():
    config = client.make_gen_config(system='s', temperature=0.3)
    assert config.system_instruction == 's'
    assert config.temperature == 0.3
    assert config.thinking_config.thinking_budget == Gemini.THINKING_BUDGET


# --- walk: the fallback ladder ----------------------------------------------

RUNGS = [('r1', 'p1'), ('r2', 'p2'), ('r3', 'p3')]


@pytest.fixture
def script(monkeypatch):
    """Scripted answers of generate_checked for walk(); records the prompts sent."""
    answers: list[tuple[str | None, str | None]] = []
    sent: list[str] = []

    async def fake(prompt, config):
        sent.append(prompt)
        return answers.pop(0)
    monkeypatch.setattr(ladder, 'generate_checked', fake)
    return answers, sent


async def _walk():
    return await ladder.walk(RUNGS, types.GenerateContentConfig(), 'gop')


async def test_walk_goes_down_while_the_input_is_blocked(script):
    answers, sent = script
    answers += [(None, BLOCK_INPUT), (None, BLOCK_INPUT), ('ok', None)]
    assert await _walk() == ('ok', 'r3')
    assert sent == ['p1', 'p2', 'p3']


async def test_walk_retries_an_output_block_on_the_same_rung(script):
    answers, sent = script
    answers += [(None, BLOCK_OUTPUT), ('ok', None)]
    assert await _walk() == ('ok', 'r1')
    assert sent == ['p1', 'p1']


async def test_walk_jumps_to_the_last_rung_on_an_empty_answer(script):
    answers, sent = script
    answers += [(None, EMPTY), ('ok', None)]
    assert await _walk() == ('ok', 'r3')
    assert sent == ['p1', 'p3']


async def test_walk_jumps_to_the_last_rung_on_an_api_error(script):
    """A 400 may be about the size of the prompt: the narrowest rung can still pass."""
    answers, sent = script
    answers += [(None, ERROR), ('ok', None)]
    assert await _walk() == ('ok', 'r3')
    assert sent == ['p1', 'p3']


async def test_walk_stops_when_gemini_does_not_answer(script):
    """A timeout or an unreachable Gemini is not about the prompt: another rung would make
    the viewer wait a second full timeout for nothing."""
    answers, sent = script
    answers += [(None, None)]
    assert await _walk() == (None, 'r1')
    assert sent == ['p1']


async def test_walk_gives_up_at_the_bottom(script):
    answers, _ = script
    answers += [(None, BLOCK_INPUT)] * 3
    assert await _walk() == (None, 'r3')


def test_identical_rungs_are_dropped():
    assert ladder.unique_rungs([('a', 'x'), ('b', 'x'), ('c', 'y')]) == [('a', 'x'), ('c', 'y')]


# --- sending to chat --------------------------------------------------------


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(Caps, 'PROBABILITY', 0.0)
    monkeypatch.setattr(Emote, 'PROBABILITY', 0.0)
    monkeypatch.setattr(responder, 'CHUNK_SEND_DELAY', 0)
    return CommandContext(
        message=make_message('сосурян привет', make_chatter('gop')), user='gop', prompt='привет',
        original_text='сосурян привет', session_id='2026-09-22 20:00', bot=FakeBot(), kind=Kind.GEMINI,
    )


async def _saved(tag: str) -> list[str]:
    async with (await get_db()).execute(
        'SELECT bot_response FROM bot_interactions WHERE user_message = ?', (tag,)
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def test_answer_goes_out_as_a_reply_and_is_saved(db, ctx):
    assert await respond_and_save(ctx, '@gop Привет!', 'привет')
    ctx.message.respond.assert_awaited_once_with('@gop Привет!')
    assert await _saved('привет') == ['Привет!']


async def test_empty_answer_sends_nothing(db, ctx):
    assert not await respond_and_save(ctx, '', 'привет')
    ctx.message.respond.assert_not_awaited()


async def test_long_answer_spreads_over_messages(db, ctx):
    text = 'Предложение номер раз и два. ' * 40
    assert await respond_and_save(ctx, text, '[versus] a vs b', max_chunks=2)
    ctx.message.respond.assert_awaited_once()
    ctx.bot.send_chat_message.assert_awaited_once()
    assert len(ctx.message.respond.await_args.args[0]) <= 450 + len('@gop ')


async def test_nothing_reached_chat_means_false(db, ctx):
    """The caller charges the per-stream limit by this result."""
    ctx.message.respond.side_effect = RuntimeError('twitch down')
    ctx.bot.send_chat_message.side_effect = RuntimeError('twitch down')
    assert not await respond_and_save(ctx, 'Раз. ' * 150, '[versus] a vs b', max_chunks=2)
    assert await _saved('[versus] a vs b') == []


async def test_send_chunked_keeps_to_the_message_limit(db, ctx):
    text = 'Это одно предложение сводки. ' * 100
    assert await send_chunked(ctx, text, '[summary]', max_chunks=2)
    sent = [ctx.message.respond.await_args.args[0], ctx.bot.send_chat_message.await_args.args[0]]
    assert sum(len(s) for s in sent) <= 2 * (450 - CHUNK_SLACK) + len('@gop ')
    assert sent[1].endswith('.')


async def test_send_chunked_says_so_when_there_is_nothing(db, ctx):
    assert not await send_chunked(ctx, None, '[summary]')
    ctx.message.respond.assert_awaited_once_with('texts.no_answer')


# --- per-stream limits ------------------------------------------------------


async def test_per_stream_limit_counts_only_what_reached_chat(db, ctx, monkeypatch):
    monkeypatch.setattr(Who, 'PER_STREAM_FOLLOWER', 1)
    run = AsyncMock(return_value=False)
    await commands._per_stream(ctx, WHO_KIND, run)
    assert await count_bot_uses('gop', WHO_KIND, 60) == 0

    run.return_value = True
    await commands._per_stream(ctx, WHO_KIND, run)
    assert await count_bot_uses('gop', WHO_KIND, 60) == 1

    run.reset_mock()
    await commands._per_stream(ctx, WHO_KIND, run)
    run.assert_not_awaited()
    ctx.message.respond.assert_awaited_with('texts.who_no_left')


async def test_one_command_per_viewer_at_a_time(db, ctx):
    commands._busy[WHO_KIND].add('gop')
    run = AsyncMock(return_value=True)
    await commands._per_stream(ctx, WHO_KIND, run)
    run.assert_not_awaited()


async def test_a_failing_command_answers_with_the_error_text(db, ctx):
    await commands._per_stream(ctx, WHO_KIND, AsyncMock(side_effect=RuntimeError), error='who_failed')
    ctx.message.respond.assert_awaited_with('texts.who_failed')
    assert 'gop' not in commands._busy[WHO_KIND]
