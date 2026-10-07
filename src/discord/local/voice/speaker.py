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
# Playback that has not ended by then is stopped: synthesis is bounded by VOICE_TIMEOUT,
# and an answer plays no longer than it took to arrive, so only a stuck player gets here
PLAY_TIMEOUT_FACTOR = 2


def prebuffer_seconds(phrase: str) -> float:
    """Speech to gather before playback: a share of the expected length, never below the floor."""
    return max(Voice.PREBUFFER_SECONDS, len(phrase) / CHARS_PER_SECOND * Voice.PREBUFFER_SHARE)


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
        fetch = asyncio.create_task(self._fetch(phrase, source, ready, prebuffer_seconds(phrase)))
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
            logger.info('Озвучка: %d симв., %.1f с, провалов %.1f с', len(phrase),
                        time.monotonic() - started, source.underruns * FRAME_SECONDS)
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
        try:
            async for chunk in self._tts.stream(phrase):
                source.feed(upsampler.convert(chunk))
                if source.buffered_seconds >= prebuffer:
                    ready.set()
        finally:
            source.finish()
            ready.set()
