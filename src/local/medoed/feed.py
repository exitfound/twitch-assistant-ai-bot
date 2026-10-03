"""SSE feed of the medoed mood: the OBS page listens, the bot pushes every change.

GET /medoed/events – text/event-stream, event `mood`, data {"mood", "rate", "chatters"};
the current state goes out on connect, so a reloaded overlay picks up where it was.
GET /medoed/state – the same JSON once, for a quick look from a browser.
"""
import asyncio
import json
import logging

from aiohttp import web

from src.core.config import Medoed
from src.local.medoed.mood import tracker

logger = logging.getLogger(__name__)

TICK_SECONDS = 5        # how often the mood is recounted without new messages (the walk down)
PING_SECONDS = 15       # a comment line keeps idle connections and proxies from timing out
QUEUE_SIZE = 16         # a stuck client loses old states, never blocks the others

_clients: set[asyncio.Queue] = set()


def on_chat(nick: str) -> None:
    """A chat message from anyone, the streamer and the bot included."""
    if not Medoed.ENABLED:
        return
    tracker.saw(nick)
    if tracker.update():
        _publish()


def _publish() -> None:
    data = json.dumps(tracker.state())
    logger.info('Медоед: %s (пишущих за окно: %d, скорость %.1f)', tracker.mood, tracker.chatters, tracker.rate)
    for queue in _clients:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(data)


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
                await response.write(b': ping\n\n')
                continue
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
    app.router.add_get('/medoed/events', _events)
    app.router.add_get('/medoed/state', _state)
    return app


async def medoed_loop() -> None:
    """Serve the feed and recount the mood until cancelled."""
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, Medoed.HOST, Medoed.PORT).start()
    except OSError:
        logger.exception('Медоед: порт %s:%d не открылся – оверлей не получит настроение', Medoed.HOST, Medoed.PORT)
        await runner.cleanup()
        return
    logger.info('Медоед: настроение для оверлея на http://%s:%d/medoed/events', Medoed.HOST, Medoed.PORT)
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if tracker.update():
                _publish()
    finally:
        await runner.cleanup()
