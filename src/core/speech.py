"""What the bot says aloud: a platform publishes its answer here, a voice feature listens.

The platforms never import each other: Twitch publishes an answer it has put in chat,
the Discord voice queues it. Publishing never blocks and never raises – a listener only
queues the text, and a failing listener is logged, not passed on to the answer. The voice
runs as a service of its own: in the bot's process a Forwarder listens and sends each
answer to it, and there the service's API publishes it again for the speaker.
"""
import asyncio
import logging
from collections.abc import Callable

from src.core.service import ServiceClient, ServiceUnavailable
from src.core.utils import put_latest

logger = logging.getLogger(__name__)

# (text, source) – source names the platform the answer went to, e.g. 'twitch'
Listener = Callable[[str, str], None]

_listeners: list[Listener] = []


def listen(listener: Listener) -> Callable[[], None]:
    """Add a listener; returns the call that removes it."""
    _listeners.append(listener)

    def remove() -> None:
        if listener in _listeners:
            _listeners.remove(listener)
    return remove


def say(text: str, source: str) -> None:
    """Hand an answer that reached chat to every listener."""
    if not text:
        return
    # A copy: a listener may remove itself while the answer is being handed out
    for listener in _listeners.copy():
        try:
            listener(text, source)
        except Exception:
            logger.exception('Озвучка: слушатель не принял реплику')


# Answers waiting for the voice service: one that is down loses the oldest, as the
# speaker itself drops what waited too long
FORWARD_QUEUE = 16
FORWARD_SECONDS = 3


class Forwarder:
    """Sends every answer that reached chat to the voice service, in order, never blocking."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self._client = ServiceClient('голос', url, token)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(FORWARD_QUEUE)
        self._down = False
        self._remove: Callable[[], None] | None = None

    def submit(self, text: str, source: str) -> None:
        put_latest(self._queue, (text, source))

    async def loop(self) -> None:
        """Listen for as long as the bot runs; the listener goes with the loop."""
        self._remove = listen(self.submit)
        try:
            while True:
                text, source = await self._queue.get()
                try:
                    await self._send(text, source)
                finally:
                    self._queue.task_done()
        finally:
            self._remove()
            await self._client.close()

    async def _send(self, text: str, source: str) -> None:
        try:
            await self._client.call('POST', '/speech', {'text': text, 'source': source}, seconds=FORWARD_SECONDS)
        except ServiceUnavailable as e:
            if not self._down:
                logger.warning('Голос недоступен – реплики не озвучиваются: %s', e)
                self._down = True
            return
        if self._down:
            logger.info('Голос снова на связи')
            self._down = False
