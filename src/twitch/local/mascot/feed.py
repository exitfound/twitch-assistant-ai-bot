"""SSE feed of the mascot mood: the OBS page listens, the bot pushes every change.

GET /mascot/events – text/event-stream, event `mood`, data {"mood", "rate", "chatters"};
the current state goes out on connect (src/twitch/core/sse.py).
GET /mascot/state – the same JSON once, for a quick look from a browser.
"""
import asyncio
import logging

from aiohttp import web

from src.core.config import Mascot
from src.twitch.core.sse import Feed
from src.twitch.local.mascot.mood import tracker

logger = logging.getLogger(__name__)

TICK_SECONDS = 5        # how often the mood is recounted without new messages (the walk down)

# The lambda reads the module's tracker on every call: the tests replace it
feed = Feed('mood', lambda: tracker.state())


def on_chat(nick: str) -> None:
    """A chat message from anyone, the streamer and the bot included."""
    if not Mascot.ENABLED:
        return
    tracker.saw(nick)
    if tracker.update():
        _publish()


def _publish() -> None:
    logger.info('Маскот: %s (пишущих за окно: %d, скорость %.1f)', tracker.mood, tracker.chatters, tracker.rate)
    feed.publish(tracker.state())


def add_routes(app: web.Application) -> None:
    app.router.add_get('/mascot/events', feed.events)
    app.router.add_get('/mascot/state', feed.state)


async def mascot_loop() -> None:
    """Recount the mood without new messages until cancelled: the walk down."""
    while True:
        await asyncio.sleep(TICK_SECONDS)
        if tracker.update():
            _publish()
