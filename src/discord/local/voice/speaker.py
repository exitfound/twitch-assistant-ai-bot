"""The voice queue: answers are spoken one at a time in the voice channel the bot is in."""
import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

import discord

from src.core.config import Voice
from src.core.paths import ACCENT_PATH
from src.discord.local.voice.audio import StreamSource, TimeStretch, Upsampler
from src.discord.local.voice import stress
from src.discord.local.voice.text import sentences, spoken
from src.discord.local.voice.tts import TTSClient, TTSUnavailable

logger = logging.getLogger(__name__)

# A server that is off fails every answer: say so in the log once per this many seconds
UNAVAILABLE_LOG_SECONDS = 300
# How fast the voice speaks, for the expected length of an answer: 280–290 characters
# took 17–20 s. A faster voice only makes the estimate, and so the prebuffer, generous
CHARS_PER_SECOND = 14.5
FRAME_SECONDS = 0.02
# The server's speed – seconds of synthesis per second of speech – drifts with its load
# between 0.8 and 1.8, so it is measured on every answer. The estimate starts here,
# follows a slowdown at once and a speedup gradually
RTF_START = 1.35
RTF_EASE = 0.3
# Headroom over the measured speed in the prebuffer. The speed is measured from the first
# byte of speech to the last: before it the request may wait behind another in the
# server's queue, which says nothing about generation. Answers with less speech than this
# after the first chunk do not count, and one measurement is capped – a stall must not
# make every following answer wait for its whole synthesis
RTF_SAFETY = 1.15
RTF_MIN_SPEECH_SECONDS = 2.0
RTF_MAX = 3.0
# After this much silence the server may run 20–50 % slower for a minute or two, and an
# answer then pauses near its end: for COLD_SECONDS the prebuffer gains
# COLD_HEADROOM_SECONDS, which covers the short pauses at the cost of a later start
COLD_AFTER_SECONDS = 180
COLD_SECONDS = 180
COLD_HEADROOM_SECONDS = 1.0
# Raw speech from the server per second: 24 kHz mono 16-bit
SERVER_BYTES_PER_SECOND = 48_000
# Playback that has not ended by then is stopped: synthesis is bounded by VOICE_TIMEOUT,
# and an answer plays no longer than it took to arrive, so only a stuck player gets here
PLAY_TIMEOUT_FACTOR = 2
# Sentence by sentence (VOICE_SENTENCES): a part is at least this long, and the parts are
# joined by this much silence on top of the pause the model leaves at a sentence end
SENTENCE_MIN_CHARS = 40
SENTENCE_PAUSE_SECONDS = 0.15
SENTENCE_PAUSE = bytes(round(SENTENCE_PAUSE_SECONDS * SERVER_BYTES_PER_SECOND / 2) * 2)


def prebuffer_seconds(phrase: str, rtf: float, cold: bool = False) -> float:
    """Speech to gather before playback so that it does not run dry at this server speed.

    At rtf r an answer of length L finishes arriving at r·L, so playback may start once
    L·(1 − 1/r) is in. Never below VOICE_PREBUFFER_SECONDS or VOICE_PREBUFFER_SHARE of L.
    VOICE_TEMPO t plays L/t seconds, which arrive as if the server ran at r·t. A cold
    server (after silence) adds COLD_HEADROOM_SECONDS.
    """
    length = len(phrase) / CHARS_PER_SECOND / Voice.TEMPO
    rtf *= Voice.TEMPO
    need = length * max(0.0, 1 - 1 / rtf) * RTF_SAFETY if rtf > 0 else 0.0
    prebuffer = max(Voice.PREBUFFER_SECONDS, length * Voice.PREBUFFER_SHARE, need)
    return prebuffer + COLD_HEADROOM_SECONDS if cold else prebuffer


class Speaker:
    """Takes answers from src/core/speech.py and plays them through the voice client.

    An answer is dropped, not kept for later, when the voice is off, the bot is not in a
    voice channel, the queue is full or it waited longer than VOICE_MAX_WAIT_SECONDS:
    a remark voiced long after it was written in chat makes no sense.
    """

    def __init__(self, voice_client: Callable[[], discord.VoiceClient | None], tts: TTSClient) -> None:
        self._voice_client = voice_client
        self._tts = tts
        # (answer, when it was queued on the monotonic clock)
        self._queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=Voice.QUEUE)
        self.enabled = Voice.ENABLED
        # The monotonic clock counts from the host's boot: zero would hide the first failure
        self._unavailable_logged = float('-inf')
        self.rtf = RTF_START
        # When the last answer finished playing, and until when the server counts as cold
        self._spoke_at = float('-inf')
        self._cold_until = float('-inf')
        self._loading: asyncio.Task | None = None

    def _connected(self) -> discord.VoiceClient | None:
        client = self._voice_client()
        return client if client is not None and client.is_connected() else None

    def submit(self, text: str, source: str) -> None:
        """A listener of src/core/speech.py: queue the answer or drop it. Never blocks."""
        if not self.enabled or (source == 'twitch' and not Voice.TWITCH) or self._connected() is None:
            return
        try:
            self._queue.put_nowait((text, time.monotonic()))
        except asyncio.QueueFull:
            logger.info('Озвучка: очередь полна (%d), реплика пропущена', Voice.QUEUE)

    def clear(self) -> None:
        """Drop everything waiting: the voice was turned off or the bot left the channel."""
        while not self._queue.empty():
            self._queue.get_nowait()

    async def run(self) -> None:
        """The worker: one answer at a time, for as long as the bot runs."""
        if Voice.YO or Voice.STRESS:
            # Beside the queue, not before it: answers until then are voiced without marks
            self._loading = asyncio.create_task(self._load_stress(), name='voice-stress')
        while True:
            text, queued_at = await self._queue.get()
            waited = time.monotonic() - queued_at
            if waited > Voice.MAX_WAIT_SECONDS:
                logger.info('Озвучка: реплика ждала %.0f с – устарела, пропущена', waited)
                continue
            try:
                await self.speak(text)
            except TTSUnavailable as e:
                now = time.monotonic()
                if now - self._unavailable_logged >= UNAVAILABLE_LOG_SECONDS:
                    self._unavailable_logged = now
                    logger.warning('Озвучка: TTS-сервер недоступен (%s) – бот отвечает только текстом', e)
            except Exception:
                logger.exception('Озвучка: реплика не прозвучала')

    async def _load_stress(self) -> None:
        """ruaccent, once per process: a second, longer outside the image, which downloads its models."""
        try:
            await asyncio.to_thread(stress.load, ACCENT_PATH)
        except Exception as e:
            logger.error('Озвучка: ruaccent не загружен (%s) – ударения и ё не расставляются', e)
            return
        logger.info('Озвучка: ruaccent загружен – %s', 'ё и ударения' if Voice.STRESS else 'ё')

    async def speak(self, text: str) -> None:
        """Synthesize and play one answer; returns when it has been played."""
        if not self.enabled:
            return
        phrase = spoken(text, Voice.MAX_CHARS)
        if not phrase or self._connected() is None:
            return
        # Cut before the stresses: a marked word at a sentence start loses its capital
        parts = sentences(phrase, SENTENCE_MIN_CHARS) if Voice.SENTENCES else [phrase]
        if (Voice.YO or Voice.STRESS) and stress.loaded():
            parts = await asyncio.to_thread(lambda: [stress.apply(part, Voice.STRESS) for part in parts])
        source = StreamSource()
        ready = asyncio.Event()
        now = time.monotonic()
        if now - self._spoke_at >= COLD_AFTER_SECONDS:
            self._cold_until = now + COLD_SECONDS
        cold = now < self._cold_until
        prebuffer = prebuffer_seconds(phrase, self.rtf, cold)
        # Text is trimmed before numbers and Latin are spelled out, so the speech can run
        # longer than VOICE_MAX_CHARS suggests: the bound grows with the phrase
        total = max(Voice.TIMEOUT, len(phrase) / CHARS_PER_SECOND * RTF_MAX + 30)
        fetch = asyncio.create_task(self._fetch(parts, source, ready, prebuffer, total))
        try:
            await ready.wait()
            if source.empty:
                # Nothing came: the server's error is in the task
                await fetch
                return
            client = self._connected()
            if client is None or not self.enabled:
                # The bot left or the voice was turned off meanwhile: the synthesis is
                # cancelled below rather than waited out on the GPU
                return
            started = time.monotonic()
            played = asyncio.Event()
            loop = asyncio.get_running_loop()

            def after(error: Exception | None) -> None:
                # discord.py's player thread: errors reach nobody unless logged here
                if error is not None:
                    logger.error('Озвучка: ошибка плеера: %s', error)
                loop.call_soon_threadsafe(played.set)

            client.play(source, after=after)
            try:
                await asyncio.wait_for(played.wait(), Voice.TIMEOUT * PLAY_TIMEOUT_FACTOR)
            except TimeoutError:
                logger.warning('Озвучка: воспроизведение не закончилось за %d с – остановлено',
                               Voice.TIMEOUT * PLAY_TIMEOUT_FACTOR)
                client.stop()
                return
            self._spoke_at = time.monotonic()
            logger.info('Озвучка: %d симв., %.1f с, запас %.1f с%s, пауз %d (%.1f с), скорость сервера %.2f',
                        len(phrase), self._spoke_at - started, prebuffer, ' после тишины' if cold else '',
                        source.stalls, source.underruns * FRAME_SECONDS, self.rtf)
            if fetch.done():
                # Raises if the server broke off mid-answer; a playback stopped early
                # (!voice turned it off, the bot left) leaves the task running – cancelled below
                await fetch
        finally:
            if not fetch.done():
                fetch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await fetch
            elif not fetch.cancelled():
                # An early return leaves a failed synthesis unawaited: taking its exception
                # keeps asyncio from logging it as never retrieved
                fetch.exception()

    async def _fetch(self, parts: list[str], source: StreamSource, ready: asyncio.Event, prebuffer: float,
                     total: float) -> None:
        upsampler = Upsampler()
        deadline = time.monotonic() + total
        first_at: float | None = None
        first_bytes = received = 0
        try:
            for n, part in enumerate(parts):
                if n:
                    source.feed(upsampler.convert(SENTENCE_PAUSE))
                stretch = TimeStretch(Voice.TEMPO)
                async for chunk in self._tts.stream(part, max(1.0, deadline - time.monotonic())):
                    if first_at is None:
                        first_at, first_bytes = time.monotonic(), len(chunk)
                    received += len(chunk)
                    source.feed(upsampler.convert(stretch.convert(chunk)))
                    if source.buffered_seconds >= prebuffer:
                        ready.set()
                source.feed(upsampler.convert(stretch.flush()))
            if first_at is not None:
                self._measure(time.monotonic() - first_at, (received - first_bytes) / SERVER_BYTES_PER_SECOND)
        finally:
            source.finish()
            ready.set()

    def _measure(self, synthesis: float, speech: float) -> None:
        """Fold one answer's speed into the estimate: a slowdown at once, a speedup gradually."""
        if speech < RTF_MIN_SPEECH_SECONDS:
            return
        rtf = min(synthesis / speech, RTF_MAX)
        self.rtf = rtf if rtf > self.rtf else self.rtf + RTF_EASE * (rtf - self.rtf)
