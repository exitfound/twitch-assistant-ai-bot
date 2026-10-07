"""The TTS server: one answer in, raw speech streamed back as it is generated."""
import asyncio
from collections.abc import AsyncIterator

import aiohttp

from src.core.config import Voice

# 16-bit mono PCM at this rate: what the server sends for response_format=pcm
RATE = 24000
# The server picks the model by its own config; the name only has to be one it accepts
MODEL = 'tts-1-ru'
# A server that is off is noticed at once, not after the whole TIMEOUT
CONNECT_TIMEOUT = 5
# The first byte may wait behind another request in the server's queue, or behind its
# warm-up after a start (about 90 s). After it, a server that stops sending is given up
# after READ_TIMEOUT of silence: a clone that missed its end of speech can keep it busy for
# minutes, and dropping the connection makes the server stop that generation at its next chunk
FIRST_BYTE_TIMEOUT = 100
READ_TIMEOUT = 30


class TTSUnavailable(Exception):
    """The server is off, unreachable or refused the request."""


class TTSClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, voice: str) -> None:
        self._session = session
        self._url = url.rstrip('/') + '/v1/audio/speech'
        self._voice = voice

    async def stream(self, text: str, total: float | None = None) -> AsyncIterator[bytes]:
        """Speech for the text as PCM chunks of any size, the first within about a second.

        total bounds the whole answer, VOICE_TIMEOUT by default.
        """
        body = {
            'model': MODEL, 'voice': self._voice, 'input': text,
            'response_format': 'pcm', 'stream': True,
            # English normalization garbles Russian: the text comes prepared (text.py)
            'normalization_options': {'normalize': False},
        }
        timeout = aiohttp.ClientTimeout(total=total or Voice.TIMEOUT, connect=CONNECT_TIMEOUT)
        try:
            async with self._session.post(self._url, json=body, timeout=timeout) as response:
                if response.status != 200:
                    detail = (await response.text())[:200]
                    raise TTSUnavailable(f'HTTP {response.status}: {detail}')
                chunks = response.content.iter_any().__aiter__()
                wait = FIRST_BYTE_TIMEOUT
                while True:
                    try:
                        chunk = await asyncio.wait_for(chunks.__anext__(), wait)
                    except StopAsyncIteration:
                        return
                    wait = READ_TIMEOUT
                    yield chunk
        except (aiohttp.ClientError, TimeoutError) as e:
            raise TTSUnavailable(str(e) or type(e).__name__) from e
