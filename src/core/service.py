"""Calls between the bot's services over their internal HTTP APIs.

A service is reached by URL on the compose network; its API is never published to the
host. Every call is JSON both ways. The caller decides what a failure means, so a client
raises one error type and says whether the request surely never reached the other side
or may have been carried out there: a slow answer, a dropped connection, a server error.
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
    """The service did not answer. maybe_done – the request went out, and the work may
    have been done all the same; False – it never reached the service."""

    def __init__(self, message: str, *, maybe_done: bool = False) -> None:
        super().__init__(message)
        self.maybe_done = maybe_done


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
                    # A server error may come after the handler did its work; a refusal never does
                    raise ServiceUnavailable(f'{self.name} {method} {path}: HTTP {resp.status}',
                                             maybe_done=resp.status >= 500)
                return await resp.json()
        # Not connected, so nothing was sent. The connect timeout is a TimeoutError too: first
        except (aiohttp.ConnectionTimeoutError, aiohttp.ClientConnectorError) as e:
            raise ServiceUnavailable(f'{self.name} {method} {path}: {_reason(e)}') from e
        except TimeoutError as e:
            raise ServiceUnavailable(f'{self.name} {method} {path}: нет ответа за {seconds} с',
                                     maybe_done=True) from e
        # Sent, then the connection dropped or the answer broke: the work may be done
        except (aiohttp.ClientError, ValueError) as e:
            raise ServiceUnavailable(f'{self.name} {method} {path}: {_reason(e)}', maybe_done=True) from e

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _reason(error: Exception) -> str:
    return str(error) or type(error).__name__


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
