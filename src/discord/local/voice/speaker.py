"""The voice queue: answers are spoken one at a time in the voice channel the bot is in."""
import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

import discord

from src.core.config import Voice
from src.discord.local.voice.audio import StreamSource, Upsampler
from src.discord.local.voice.text import spoken
from src.discord.local.voice.tts import TTSClient, TTSUnavailable

logger = logging.getLogger(__name__)

# A server that is off fails every answer: say so in the log once per this many seconds
UNAVAILABLE_LOG_SECONDS = 300
# How fast the voice speaks, for the expected length of an answer: 280–290 characters
# took 17–20 s. A faster voice only makes the estimate, and so the prebuffer, generous
CHARS_PER_SECOND = 14.5
FRAME_SECONDS = 0.02
# The server's speed – seconds of synthesis per second of speech – drifts with its load and
# state (1.1–1.7 were seen on one evening), so it is measured on every answer. The estimate
# starts here, follows a slowdown at once and a speedup gradually
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
# Raw speech from the server per second: 24 kHz mono 16-bit
SERVER_BYTES_PER_SECOND = 48_000
# Playback that has not ended by then is stopped: synthesis is bounded by VOICE_TIMEOUT,
# and an answer plays no longer than it took to arrive, so only a stuck player gets here
PLAY_TIMEOUT_FACTOR = 2


def prebuffer_seconds(phrase: str, rtf: float) -> float:
    """Speech to gather before playback so that it does not run dry at this server speed.

    At rtf r an answer of length L finishes arriving at r·L, so playback may start once
    L·(1 − 1/r) is in. Never below VOICE_PREBUFFER_SECONDS or VOICE_PREBUFFER_SHARE of L.
    """
    length = len(phrase) / CHARS_PER_SECOND
    need = length * max(0.0, 1 - 1 / rtf) * RTF_SAFETY if rtf > 0 else 0.0
    return max(Voice.PREBUFFER_SECONDS, length * Voice.PREBUFFER_SHARE, need)


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
        self._unavailable_logged = 0.0
        self.rtf = RTF_START

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

    async def speak(self, text: str) -> None:
        """Synthesize and play one answer; returns when it has been played."""
        if not self.enabled:
            return
        phrase = spoken(text, Voice.MAX_CHARS)
        if not phrase or self._connected() is None:
            return
        source = StreamSource()
        ready = asyncio.Event()
        prebuffer = prebuffer_seconds(phrase, self.rtf)
        fetch = asyncio.create_task(self._fetch(phrase, source, ready, prebuffer))
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
            logger.info('Озвучка: %d симв., %.1f с, запас %.1f с, пауз %d (%.1f с), скорость сервера %.2f',
                        len(phrase), time.monotonic() - started, prebuffer, source.stalls,
                        source.underruns * FRAME_SECONDS, self.rtf)
            if fetch.done():
                # Raises if the server broke off mid-answer; a playback stopped early
                # (!voice turned it off, the bot left) leaves the task running – cancelled below
                await fetch
        finally:
            if not fetch.done():
                fetch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await fetch

    async def _fetch(self, phrase: str, source: StreamSource, ready: asyncio.Event, prebuffer: float) -> None:
        upsampler = Upsampler()
        first_at: float | None = None
        first_bytes = received = 0
        try:
            async for chunk in self._tts.stream(phrase):
                if first_at is None:
                    first_at, first_bytes = time.monotonic(), len(chunk)
                received += len(chunk)
                source.feed(upsampler.convert(chunk))
                if source.buffered_seconds >= prebuffer:
                    ready.set()
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
