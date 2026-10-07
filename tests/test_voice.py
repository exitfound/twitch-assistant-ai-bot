"""The Discord voice without Discord and without the TTS server."""
import asyncio
import logging
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.core import config, speech
from src.core.config import Discord, Voice
from src.core.database import get_state, set_state
from src.discord import bot as discord_module
from src.discord.bot import Move, presence
from src.discord.local.voice import speaker as speaker_module
from src.discord.local.voice.audio import FRAME_BYTES, SILENCE, StreamSource, Upsampler
from src.discord.local.voice.speaker import Speaker, prebuffer_seconds
from src.discord.local.voice.text import number_words, parse_nicks, prepare
from src.discord.local.voice.tts import TTSClient, TTSUnavailable

NICKS = {'exitfound': 'Эксит-фаунд', 'm1ndsh1ft_': 'Майнд-шифт', 'nosok222': 'Носок'}


def say(text: str, max_chars: int = 300) -> str:
    return prepare(text, NICKS, {'KEKW', 'exitfoGigaCAT'}, max_chars)


# --- text -------------------------------------------------------------------

def test_the_addressees_at_the_start_are_not_read():
    assert say('@exitfound тест это то, что мы делаем') == 'Тест это то, что мы делаем'
    assert say('@exitfound @m1ndsh1ft_? ЭТО ТОТ ТИП') == 'Это тот тип'


def test_a_nick_inside_a_sentence_is_read_without_the_at():
    assert say('@exitfound или @m1ndsh1ft_ опять проиграл') == 'Или Майнд-шифт опять проиграл'


def test_known_nicks_come_from_the_dictionary_whatever_their_case():
    assert say('пока @M1ndsh1ft_ и nosok222 спорят') == 'Пока Майнд-шифт и Носок спорят'


def test_an_unknown_latin_word_is_transliterated():
    assert say('это shit полный') == 'Это шит полный'


def test_emotes_links_and_emoji_are_not_read_aloud():
    assert say('смотри https://example.com/x KEKW 😂 exitfoGigaCAT ага') == 'Смотри ссылка ага'


def test_an_emote_with_punctuation_stuck_to_it_is_still_an_emote():
    assert say('ага KEKW, ну KEKW!') == 'Ага ну'


def test_versions_and_percents_are_read_as_words():
    assert say('опус 5.5 и 50%') == 'Опус пять точка пять и пятьдесят процентов'


def test_shouting_is_read_in_a_normal_voice():
    assert say('ПАКА ПАКА. САМ НЕ ЗАБУДЬ') == 'Пака пака. Сам не забудь'


def test_numbers_become_russian_words():
    assert say('у тебя 3 зрителя и 1000 фолловеров') == 'У тебя три зрителя и тысяча фолловеров'
    assert number_words(2024) == 'две тысячи двадцать четыре'
    assert number_words(11000) == 'одиннадцать тысяч'


def test_a_long_answer_is_cut_at_a_sentence_end():
    text = 'Первое предложение тут. ' * 30
    out = say(text, max_chars=100)
    assert len(out) <= 100 and out.endswith('.')


def test_nothing_speakable_gives_an_empty_text():
    assert say('KEKW 😂 exitfoGigaCAT') == ''


def test_nick_lines_from_content_md():
    lines = ['exitfound = Эксит-фаунд', '@Nosok222=Носок', 'кривая строка', 'x =']
    assert parse_nicks(lines) == {'exitfound': 'Эксит-фаунд', 'nosok222': 'Носок'}


# --- audio ------------------------------------------------------------------

def pcm(*samples: int) -> bytes:
    return array('h', samples).tobytes()


def test_each_sample_becomes_two_stereo_frames_with_the_midpoint_first():
    out = array('h')
    out.frombytes(Upsampler().convert(pcm(100, 200)))
    assert out.tolist() == [50, 50, 100, 100, 150, 150, 200, 200]


def test_a_sample_split_across_chunks_is_not_lost():
    up = Upsampler()
    whole = pcm(1000, -1000, 300)
    parts = up.convert(whole[:3]) + up.convert(whole[3:])
    assert parts == Upsampler().convert(whole)


def test_a_long_answer_waits_for_a_share_of_its_length_a_short_one_for_the_floor(monkeypatch):
    monkeypatch.setattr(Voice, 'PREBUFFER_SECONDS', 2.0)
    monkeypatch.setattr(Voice, 'PREBUFFER_SHARE', 0.25)
    assert prebuffer_seconds('а' * 50) == 2.0
    assert prebuffer_seconds('а' * 290) == pytest.approx(5.0)


def test_a_missing_frame_plays_as_silence_until_the_answer_ends():
    source = StreamSource()
    assert source.read() == SILENCE
    source.feed(b'\x01' * (FRAME_BYTES + 10))
    assert source.read() == b'\x01' * FRAME_BYTES
    assert source.read() == SILENCE
    source.finish()
    assert source.read() == (b'\x01' * 10).ljust(FRAME_BYTES, b'\0')
    assert source.read() == b''
    # Two frames went out as silence before the end: what the prebuffer is tuned by
    assert source.underruns == 2


# --- speaker ----------------------------------------------------------------

class FakeVoiceClient:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.played: list[bytes] = []

    def is_connected(self) -> bool:
        return self.connected

    def play(self, source, after) -> None:
        async def drain():
            while (frame := source.read()) != b'':
                self.played.append(frame)
                await asyncio.sleep(0)
            after(None)
        asyncio.get_running_loop().create_task(drain())


class FakeTTS:
    def __init__(self, chunks: list[bytes] | None = None, error: Exception | None = None) -> None:
        self.chunks = chunks or []
        self.error = error
        self.texts: list[str] = []

    async def stream(self, text: str):
        self.texts.append(text)
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def plain_text(monkeypatch):
    monkeypatch.setattr(speaker_module, 'spoken', lambda text, max_chars: text)
    monkeypatch.setattr(Voice, 'PREBUFFER_SECONDS', 0.0)


async def test_an_answer_is_synthesized_and_played(plain_text):
    client = FakeVoiceClient()
    tts = FakeTTS([pcm(*range(480))] * 4)
    await Speaker(lambda: client, tts).speak('привет')
    assert tts.texts == ['привет']
    # 4 × 480 samples at 24 kHz = 80 ms = four 20 ms frames
    assert len(client.played) == 4


async def test_playback_waits_for_the_prebuffer(plain_text, monkeypatch):
    monkeypatch.setattr(Voice, 'PREBUFFER_SECONDS', 0.05)
    order = []
    client = FakeVoiceClient()
    real_play = client.play

    def play(source, after):
        order.append(source.buffered_seconds)
        real_play(source, after)
    client.play = play
    await Speaker(lambda: client, FakeTTS([pcm(*range(480))] * 4)).speak('привет')
    assert len(order) == 1 and order[0] >= 0.05


async def test_a_dead_server_raises_without_playing(plain_text):
    client = FakeVoiceClient()
    with pytest.raises(TTSUnavailable):
        await Speaker(lambda: client, FakeTTS(error=TTSUnavailable('refused'))).speak('привет')
    assert client.played == []


async def test_the_worker_logs_a_dead_server_once_and_goes_on(plain_text, caplog):
    client = FakeVoiceClient()
    speaker = Speaker(lambda: client, FakeTTS(error=TTSUnavailable('refused')))
    speaker.submit('раз', 'twitch')
    speaker.submit('два', 'twitch')
    worker = asyncio.create_task(speaker.run())
    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            await asyncio.sleep(0)
    worker.cancel()
    assert sum('недоступен' in r.message for r in caplog.records) == 1


async def test_playback_stopped_early_cancels_the_synthesis(plain_text):
    """!voice off or leaving the channel must not leave the GPU finishing an answer nobody hears."""
    cancelled = asyncio.Event()

    class EndlessTTS:
        async def stream(self, text):
            try:
                while True:
                    yield pcm(*range(480))
                    await asyncio.sleep(0)
            finally:
                cancelled.set()

    class StoppedClient(FakeVoiceClient):
        def play(self, source, after):
            asyncio.get_running_loop().call_soon(after, None)

    await asyncio.wait_for(Speaker(lambda: StoppedClient(), EndlessTTS()).speak('привет'), 1)
    assert cancelled.is_set()


async def test_a_player_error_is_logged(plain_text, caplog):
    class BrokenClient(FakeVoiceClient):
        def play(self, source, after):
            asyncio.get_running_loop().call_soon(after, RuntimeError('opus encoder'))

    with caplog.at_level(logging.ERROR):
        await Speaker(lambda: BrokenClient(), FakeTTS([pcm(1, 2, 3)])).speak('привет')
    assert any('opus encoder' in r.message for r in caplog.records)


# --- tts client -------------------------------------------------------------

async def _tts_server(handler):
    app = web.Application()
    app.router.add_post('/v1/audio/speech', handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_the_tts_client_streams_pcm_and_sends_prepared_text():
    seen = {}

    async def handler(request):
        seen.update(await request.json())
        response = web.StreamResponse()
        await response.prepare(request)
        for part in (b'\x01\x00', b'\x02\x00'):
            await response.write(part)
        return response

    server = await _tts_server(handler)
    async with aiohttp.ClientSession() as session:
        client = TTSClient(session, str(server.make_url('/')), 'clone:kael_low')
        audio = b''.join([chunk async for chunk in client.stream('Привет')])
    await server.close()
    assert audio == b'\x01\x00\x02\x00'
    assert seen['voice'] == 'clone:kael_low' and seen['input'] == 'Привет'
    assert seen['normalization_options'] == {'normalize': False} and seen['stream'] is True


async def test_a_server_error_or_no_server_is_tts_unavailable():
    async def handler(request):
        return web.Response(status=500, text='CUDA out of memory')

    server = await _tts_server(handler)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TTSUnavailable, match='500'):
            [chunk async for chunk in TTSClient(session, str(server.make_url('/')), 'v').stream('а')]
        url = str(server.make_url('/'))
        await server.close()
        with pytest.raises(TTSUnavailable):
            [chunk async for chunk in TTSClient(session, url, 'v').stream('а')]


def test_nothing_is_queued_off_voice_or_with_the_voice_off():
    away = Speaker(lambda: FakeVoiceClient(connected=False), FakeTTS())
    away.submit('привет', 'twitch')
    assert away._queue.empty()
    muted = Speaker(lambda: FakeVoiceClient(), FakeTTS())
    muted.enabled = False
    muted.submit('привет', 'twitch')
    assert muted._queue.empty()


def test_twitch_answers_are_skipped_when_twitch_voice_is_off(monkeypatch):
    monkeypatch.setattr(Voice, 'TWITCH', False)
    speaker = Speaker(lambda: FakeVoiceClient(), FakeTTS())
    speaker.submit('привет', 'twitch')
    assert speaker._queue.empty()


def test_a_full_queue_drops_the_new_answer(monkeypatch):
    speaker = Speaker(lambda: FakeVoiceClient(), FakeTTS())
    for i in range(Voice.QUEUE + 2):
        speaker.submit(f'реплика {i}', 'twitch')
    assert speaker._queue.qsize() == Voice.QUEUE
    assert speaker._queue.get_nowait()[0] == 'реплика 0'


async def test_an_answer_that_waited_too_long_is_dropped(plain_text, monkeypatch, caplog):
    client = FakeVoiceClient()
    tts = FakeTTS([pcm(1, 2, 3)])
    speaker = Speaker(lambda: client, tts)
    clock = [1000.0]
    monkeypatch.setattr(speaker_module.time, 'monotonic', lambda: clock[0])
    speaker.submit('старая', 'twitch')
    clock[0] += Voice.MAX_WAIT_SECONDS + 1
    speaker.submit('свежая', 'twitch')
    worker = asyncio.create_task(speaker.run())
    with caplog.at_level(logging.INFO):
        for _ in range(30):
            await asyncio.sleep(0)
    worker.cancel()
    assert tts.texts == ['свежая']
    assert any('устарела' in r.message for r in caplog.records)


def test_speech_reaches_a_listener_and_survives_a_broken_one():
    heard = []

    def broken(text, source):
        raise RuntimeError('boom')
    speech.listen(broken)
    remove = speech.listen(lambda text, source: heard.append((text, source)))
    speech.say('привет', 'twitch')
    remove()
    speech.say('ещё', 'twitch')
    assert heard == [('привет', 'twitch')]


# --- presence ---------------------------------------------------------------

@pytest.mark.parametrize(('wanted', 'connected', 'move'), [
    (True, False, Move.JOIN),
    (True, True, Move.STAY),
    (False, True, Move.LEAVE),
    (False, False, Move.STAY),
])
def test_the_bot_is_in_the_channel_exactly_while_the_owner_wants_it(wanted, connected, move):
    assert presence(wanted, connected) is move


@pytest.fixture
async def discord_bot(db, monkeypatch):
    """A DiscordBot that never touches Discord: the voice channel is a flag."""
    bot = discord_module.DiscordBot()
    bot.speaker = Speaker(lambda: None, FakeTTS())
    state = {'connected': False}

    async def connect(channel):
        state['connected'] = True
        return True

    async def disconnect():
        state['connected'] = False

    monkeypatch.setattr(bot, '_connect', connect)
    monkeypatch.setattr(bot, '_disconnect', disconnect)
    monkeypatch.setattr(bot, '_voice_channel', lambda: SimpleNamespace(name='голос'))
    monkeypatch.setattr(bot, '_voice_client', lambda: SimpleNamespace(
        is_connected=lambda: state['connected'], is_playing=lambda: False))
    bot.state = state
    yield bot
    await discord.Client.close(bot)


async def test_join_and_leave_are_remembered_across_restarts(discord_bot):
    assert await discord_bot._command('!join', []) == discord_module.OK
    assert discord_bot.state['connected']
    assert await get_state(discord_module.VOICE_STATE_KEY) == 'on'
    assert await discord_bot._command('!leave', []) == discord_module.OK
    assert not discord_bot.state['connected']
    assert await get_state(discord_module.VOICE_STATE_KEY) == 'off'


async def test_a_dropped_connection_is_put_back_while_the_owner_wants_it(discord_bot):
    await set_state(discord_module.VOICE_STATE_KEY, 'on')
    await discord_bot._reconcile()
    assert discord_bot.state['connected']
    discord_bot.state['connected'] = False
    await discord_bot._reconcile()
    assert discord_bot.state['connected']


async def test_without_a_choice_the_bot_stays_out(discord_bot):
    await discord_bot._reconcile()
    assert not discord_bot.state['connected']


async def test_voice_toggles_and_the_reaction_shows_the_new_state(discord_bot):
    assert await discord_bot._command('!voice', []) == discord_module.MUTED
    assert not discord_bot.speaker.enabled
    assert await discord_bot._command('!voice', []) == discord_module.OK
    assert discord_bot.speaker.enabled


async def test_the_voice_switch_is_remembered_across_restarts(discord_bot):
    assert await discord_bot._command('!voice', []) == discord_module.MUTED
    assert await get_state(discord_module.VOICE_ENABLED_KEY) == 'off'
    restarted = discord_module.DiscordBot()
    restarted.speaker = Speaker(lambda: None, FakeTTS())
    assert restarted.speaker.enabled
    await restarted._load_state()
    assert not restarted.speaker.enabled
    await discord.Client.close(restarted)


async def test_without_a_saved_switch_the_voice_starts_as_configured(discord_bot):
    await discord_bot._load_state()
    assert discord_bot.speaker.enabled is Voice.ENABLED


def _message(text: str, author: int = 42, channel: int | None = None, bot: bool = False):
    return SimpleNamespace(
        content=text, author=SimpleNamespace(id=author, bot=bot),
        channel=SimpleNamespace(id=channel if channel is not None else Discord.TEXT_CHANNEL_ID),
        reply=AsyncMock(), add_reaction=AsyncMock(),
    )


@pytest.fixture
def discord_ids(monkeypatch):
    monkeypatch.setattr(Discord, 'TEXT_CHANNEL_ID', 111)
    monkeypatch.setattr(Discord, 'OWNER_ID', 7)


async def test_help_answers_anyone_in_the_channel_once_per_cooldown(discord_bot, discord_ids):
    first, second = _message('!help'), _message('!HELP')
    await discord_bot.on_message(first)
    await discord_bot.on_message(second)
    first.reply.assert_awaited_once()
    assert first.reply.await_args.args[0] == 'texts.discord_help'
    second.reply.assert_not_awaited()


@pytest.mark.parametrize('message', [
    pytest.param({'text': '!help', 'bot': True}, id='another bot'),
    pytest.param({'text': '!help', 'channel': 999}, id='another channel'),
])
async def test_help_ignores_bots_and_other_channels(discord_bot, discord_ids, message):
    msg = _message(**message)
    await discord_bot.on_message(msg)
    msg.reply.assert_not_awaited()


async def test_voice_commands_stay_with_the_owner(discord_bot, discord_ids):
    stranger, again, owner = _message('!join', author=42), _message('!voice', author=42), _message('!join', author=7)
    await discord_bot.on_message(stranger)
    await discord_bot.on_message(again)
    assert not discord_bot.state['connected'] and discord_bot.speaker.enabled
    stranger.add_reaction.assert_not_awaited()
    # Told once that the commands are not theirs, then quiet for the cooldown
    stranger.reply.assert_awaited_once()
    assert stranger.reply.await_args.args[0] == 'texts.discord_no_rights'
    again.reply.assert_not_awaited()
    await discord_bot.on_message(owner)
    assert discord_bot.state['connected']
    owner.add_reaction.assert_awaited_once_with(discord_module.OK)


async def test_a_member_of_a_command_role_runs_voice_commands(discord_bot, discord_ids, monkeypatch):
    monkeypatch.setattr(Discord, 'COMMAND_ROLE_IDS', frozenset({500}))
    guard = _message('!join', author=42)
    guard.author.roles = [SimpleNamespace(id=300), SimpleNamespace(id=500)]
    await discord_bot.on_message(guard)
    assert discord_bot.state['connected']
    guard.add_reaction.assert_awaited_once_with(discord_module.OK)


@pytest.mark.parametrize(('author', 'allowed'), [
    pytest.param(SimpleNamespace(id=7), True, id='owner'),
    pytest.param(SimpleNamespace(id=42, roles=[SimpleNamespace(id=500)]), True, id='role'),
    pytest.param(SimpleNamespace(id=42, roles=[SimpleNamespace(id=300)]), False, id='other role'),
    pytest.param(SimpleNamespace(id=42), False, id='no roles (a DM user)'),
])
def test_who_may_run_the_voice_commands(discord_ids, monkeypatch, author, allowed):
    monkeypatch.setattr(Discord, 'COMMAND_ROLE_IDS', frozenset({500}))
    assert discord_module.may_command(author) is allowed


def test_command_role_ids_are_read_from_a_comma_list(monkeypatch, caplog):
    monkeypatch.setenv('DISCORD_COMMAND_ROLE_IDS', ' 1516436699696070726, 1516432204467667056 ,дружина,')
    with caplog.at_level(logging.WARNING):
        assert config._env_ids('DISCORD_COMMAND_ROLE_IDS') == {1516436699696070726, 1516432204467667056}
    assert any('дружина' in r.message for r in caplog.records)


class FlakyBot:
    """DiscordBot stand-in: start() fails as told, then blocks until close()."""
    starts = 0
    failures: tuple[BaseException, ...] = ()

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def start(self, token) -> None:
        FlakyBot.starts += 1
        if FlakyBot.failures:
            error, *rest = FlakyBot.failures
            FlakyBot.failures = tuple(rest)
            raise error
        await self._closed.wait()

    async def close(self) -> None:
        self._closed.set()


@pytest.fixture
def flaky(monkeypatch):
    FlakyBot.starts = 0
    monkeypatch.setattr(discord_module, 'DiscordBot', FlakyBot)
    monkeypatch.setattr(discord_module, 'RETRY_SECONDS', 0)
    return FlakyBot


async def test_a_failed_start_is_retried_until_discord_comes_up(flaky):
    flaky.failures = (OSError('no network'), OSError('still no network'))
    service = discord_module.DiscordService()
    task = asyncio.create_task(service.run())
    for _ in range(50):
        await asyncio.sleep(0)
    assert flaky.starts == 3 and not task.done()
    await service.stop()
    await asyncio.wait_for(task, 1)


async def test_a_rejected_token_is_not_retried(flaky):
    flaky.failures = (discord.LoginFailure('bad token'),)
    await asyncio.wait_for(discord_module.DiscordService().run(), 1)
    assert flaky.starts == 1


def test_the_fake_voice_client_matches_what_the_speaker_uses():
    """The speaker relies on these members of discord.VoiceClient only."""
    for name in ('is_connected', 'play'):
        assert hasattr(discord.VoiceClient, name)
