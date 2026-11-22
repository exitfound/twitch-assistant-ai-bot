"""Calls between the bot's services over their internal HTTP APIs.

A service is reached by URL on the compose network; its API is never published to the
host. Every call is JSON both ways. The caller decides what a failure means, so a client
raises one error type and says whether the other side was unreachable or only slow:
a slow call may still have been carried out.
"""
import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

TOKEN_HEADER = 'X-Service-Token'
SHUTDOWN_SECONDS = 2


class ServiceUnavailable(Exception):
    """The service did not answer. timeout – it may have done the work all the same."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


class ServiceClient:
    """One service's API: a kept session, a token, a deadline per call."""

    def __init__(self, name: str, url: str, token: str | None = None) -> None:
        self.name = name
        self._url = url.rstrip('/')
        self._headers = {TOKEN_HEADER: token} if token else {}
        self._session: aiohttp.ClientSession | None = None

    async def call(self, method: str, path: str, body: dict | None = None, *, seconds: float = 5) -> dict:
        """The answer's JSON. Raises ServiceUnavailable on anything but a 200 with JSON."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        try:
            async with self._session.request(
                method, self._url + path, json=body, timeout=aiohttp.ClientTimeout(total=seconds, connect=2),
            ) as resp:
                if resp.status != 200:
                    raise ServiceUnavailable(f'{self.name} {method} {path}: HTTP {resp.status}')
                return await resp.json()
        except TimeoutError as e:
            raise ServiceUnavailable(f'{self.name} {method} {path}: нет ответа за {seconds} с', timeout=True) from e
        except (aiohttp.ClientError, ValueError) as e:
            raise ServiceUnavailable(f'{self.name} {method} {path}: {e or type(e).__name__}') from e

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def require_token(token: str | None) -> Callable:
    """Middleware: with a token set, a request without it is refused."""
    @web.middleware
    async def check(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if token and not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ''), token):
            return web.json_response({'error': 'forbidden'}, status=403)
        return await handler(request)
    return check


async def serve(app: web.Application, host: str, port: int, name: str,
                on_stop: Callable[[], None] | None = None) -> None:
    """Serve until cancelled. A busy port is logged and the rest of the process runs on."""
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=SHUTDOWN_SECONDS)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as e:
        logger.error('%s: порт %s:%d не открылся (%s)', name, host, port, e)
        await runner.cleanup()
        return
    logger.info('%s на http://%s:%d', name, host, port)
    try:
        await asyncio.Event().wait()
    finally:
        if on_stop is not None:
            on_stop()
        await runner.cleanup()
