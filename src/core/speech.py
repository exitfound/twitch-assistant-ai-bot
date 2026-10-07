"""What the bot says aloud: a platform publishes its answer here, a voice feature listens.

The platforms never import each other: Twitch publishes an answer it has put in chat,
the Discord voice queues it. Publishing never blocks and never raises – a listener only
queues the text, and a failing listener is logged, not passed on to the answer.
"""
import logging
from collections.abc import Callable

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
