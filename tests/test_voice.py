"""The Discord voice without Discord and without the TTS server."""
import asyncio
import logging
from array import array

import discord
import pytest

from src.core import speech
from src.core.config import Voice
from src.discord.bot import Move, presence
from src.discord.local.voice import speaker as speaker_module
from src.discord.local.voice.audio import FRAME_BYTES, SILENCE, StreamSource, Upsampler
from src.discord.local.voice.speaker import Speaker
from src.discord.local.voice.text import number_words, parse_nicks, prepare
from src.discord.local.voice.tts import TTSUnavailable

NICKS = {'exitfound': 'Эксит-фаунд', 'm1ndsh1ft_': 'Майнд-шифт', 'nosok222': 'Носок'}


def say(text: str, max_chars: int = 300) -> str:
    return prepare(text, NICKS, {'KEKW', 'exitfoGigaCAT'}, max_chars)


# --- text -------------------------------------------------------------------

def test_a_reply_starts_with_the_nick_said_in_russian_and_a_pause():
    assert say('@exitfound тест это то, что мы делаем') == 'Эксит-фаунд, тест это то, что мы делаем'


def test_known_nicks_come_from_the_dictionary_whatever_their_case():
    assert say('пока @M1ndsh1ft_ и nosok222 спорят') == 'Пока Майнд-шифт и Носок спорят'


def test_an_unknown_latin_word_is_transliterated():
    assert say('это shit полный') == 'Это шит полный'


def test_emotes_links_and_emoji_are_not_read_aloud():
    assert say('смотри https://example.com/x KEKW 😂 exitfoGigaCAT ага') == 'Смотри ссылка ага'


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


def test_a_missing_frame_plays_as_silence_until_the_answer_ends():
    source = StreamSource()
    assert source.read() == SILENCE
    source.feed(b'\x01' * (FRAME_BYTES + 10))
    assert source.read() == b'\x01' * FRAME_BYTES
    assert source.read() == SILENCE
    source.finish()
    assert source.read() == (b'\x01' * 10).ljust(FRAME_BYTES, b'\0')
    assert source.read() == b''


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
    assert speaker._queue.get_nowait() == 'реплика 0'


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

@pytest.mark.parametrize(('connected', 'owner', 'people', 'held_off', 'move'), [
    (False, True, True, False, Move.JOIN),
    (False, True, True, True, Move.STAY),
    (False, False, True, False, Move.STAY),
    (True, False, True, False, Move.STAY),
    (True, False, False, False, Move.LEAVE),
    (True, True, True, False, Move.STAY),
])
def test_the_bot_follows_the_owner_and_never_sits_alone(connected, owner, people, held_off, move):
    assert presence(connected, owner, people, held_off) is move


def test_the_fake_voice_client_matches_what_the_speaker_uses():
    """The speaker relies on these members of discord.VoiceClient only."""
    for name in ('is_connected', 'play'):
        assert hasattr(discord.VoiceClient, name)
