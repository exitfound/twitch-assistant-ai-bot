"""Server-sent events for the OBS overlays: a page listens, the bot pushes every change.

One aiohttp server on MASCOT_HOST:MASCOT_PORT carries every overlay's routes. A feed sends
its current state on connect, so a reloaded page picks up where it was, and `ping` every
PING_SECONDS of silence, so the page can tell a dead connection.
"""
import asyncio
import json
import logging
from collections.abc import Callable, Iterable

from aiohttp import web

logger = logging.getLogger(__name__)

PING_SECONDS = 15       # keeps idle connections alive; the page reconnects after a longer silence
QUEUE_SIZE = 16         # a stuck client loses old items, never blocks the others
# A stream never ends by itself: without a stop signal the server's shutdown waits for every
# open page (60 s by default), longer than the 30 s docker gives the whole bot to stop
STOP = None
SHUTDOWN_SECONDS = 2
# Pages are local files in OBS or served from another origin
CORS = {'Access-Control-Allow-Origin': '*'}


class Feed:
    """One SSE event name, its listeners and the state a new listener starts from."""

    def __init__(self, event: str, state: Callable[[], dict]) -> None:
        self.event = event
        self._state = state
        self.clients: set[asyncio.Queue] = set()

    def publish(self, data: dict) -> None:
        self.broadcast(json.dumps(data, ensure_ascii=False))

    def broadcast(self, item: str | None) -> None:
        for queue in self.clients:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    def stop(self) -> None:
        self.broadcast(STOP)

    async def events(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={
            'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', **CORS,
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self.clients.add(queue)
        try:
            await self._send(response, json.dumps(self._state(), ensure_ascii=False))
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), PING_SECONDS)
                except TimeoutError:
                    await response.write(b'event: ping\ndata: {}\n\n')
                    continue
                if data is STOP:
                    break
                await self._send(response, data)
        except ConnectionResetError:
            pass
        finally:
            self.clients.discard(queue)
        return response

    async def state(self, _request: web.Request) -> web.Response:
        return web.json_response(self._state(), headers=CORS, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def _send(self, response: web.StreamResponse, data: str) -> None:
        await response.write(f'event: {self.event}\ndata: {data}\n\n'.encode())


async def serve(app: web.Application, feeds: Iterable[Feed], host: str, port: int) -> None:
    """Serve the overlays until cancelled. A busy port is logged, the rest of the bot runs on."""
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=SHUTDOWN_SECONDS)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as e:
        logger.error('Оверлеи: порт %s:%d не открылся (%s) – OBS ничего не получит', host, port, e)
        await runner.cleanup()
        return
    logger.info('Оверлеи для OBS на http://%s:%d', host, port)
    try:
        await asyncio.Event().wait()
    finally:
        # open pages end their streams first, then the server stops at once
        for feed in feeds:
            feed.stop()
        await runner.cleanup()
