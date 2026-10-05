"""SSE feed of the mascot mood: the OBS page listens, the bot pushes every change.

GET /mascot/events – text/event-stream, event `mood`, data {"mood", "rate", "chatters"};
the current state goes out on connect, so a reloaded overlay picks up where it was, and
event `ping` every PING_SECONDS of silence, so the overlay can tell a dead connection.
GET /mascot/state – the same JSON once, for a quick look from a browser.
"""
import asyncio
import json
import logging

from aiohttp import web

from src.core.config import Mascot
from src.local.mascot.mood import tracker

logger = logging.getLogger(__name__)

TICK_SECONDS = 5        # how often the mood is recounted without new messages (the walk down)
PING_SECONDS = 15       # keeps idle connections alive; the overlay reconnects after a longer silence
QUEUE_SIZE = 16         # a stuck client loses old states, never blocks the others
# A stream never ends by itself: without a stop signal the server's shutdown waits for every
# open overlay (60 s by default), longer than the 30 s docker gives the whole bot to stop
STOP = None
SHUTDOWN_SECONDS = 2

_clients: set[asyncio.Queue] = set()


def on_chat(nick: str) -> None:
    """A chat message from anyone, the streamer and the bot included."""
    if not Mascot.ENABLED:
        return
    tracker.saw(nick)
    if tracker.update():
        _publish()


def _publish() -> None:
    logger.info('Маскот: %s (пишущих за окно: %d, скорость %.1f)', tracker.mood, tracker.chatters, tracker.rate)
    _broadcast(json.dumps(tracker.state()))


def _broadcast(item: str | None) -> None:
    for queue in _clients:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(item)


async def _events(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        # the overlay is a local file in OBS: its origin is not this server
        'Access-Control-Allow-Origin': '*',
    })
    await response.prepare(request)
    queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
    _clients.add(queue)
    try:
        await _send(response, json.dumps(tracker.state()))
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                await response.write(b'event: ping\ndata: {}\n\n')
                continue
            if data is STOP:
                break
            await _send(response, data)
    except ConnectionResetError:
        pass
    finally:
        _clients.discard(queue)
    return response


async def _send(response: web.StreamResponse, data: str) -> None:
    await response.write(f'event: mood\ndata: {data}\n\n'.encode())


async def _state(_request: web.Request) -> web.Response:
    return web.json_response(tracker.state(), headers={'Access-Control-Allow-Origin': '*'})


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get('/mascot/events', _events)
    app.router.add_get('/mascot/state', _state)
    return app


async def mascot_loop() -> None:
    """Serve the feed and recount the mood until cancelled."""
    runner = web.AppRunner(make_app(), access_log=None, shutdown_timeout=SHUTDOWN_SECONDS)
    await runner.setup()
    try:
        await web.TCPSite(runner, Mascot.HOST, Mascot.PORT).start()
    except OSError as e:
        logger.error('Маскот: порт %s:%d не открылся (%s) – оверлей не получит настроение', Mascot.HOST, Mascot.PORT, e)
        await runner.cleanup()
        return
    logger.info('Маскот: настроение для оверлея на http://%s:%d/mascot/events', Mascot.HOST, Mascot.PORT)
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if tracker.update():
                _publish()
    finally:
        # open overlays end their streams first, then the server stops at once
        _broadcast(STOP)
        await runner.cleanup()
