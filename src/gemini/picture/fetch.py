"""Downloading a picture from a link in chat.

The link comes from a random viewer, so there are more checks than code. A link to an
internal address would turn the bot into a way of knocking on the host's local network
(SSRF), so every IP the name resolves to is checked, redirects are followed by hand –
automatic following would reach a private address after the check – and the address of
the established connection is checked as well, because httpx resolves the name a second
time and a zero-TTL record could swap it in between.

Also: http/https only, image/* only, a timeout and a size cap.
"""
import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from src.core.config import Picture

logger = logging.getLogger(__name__)

# Without a User-Agent some hosts (Wikimedia, Reddit) return 403
USER_AGENT = 'sosuryan-twitch-bot/1.0 (+https://twitch.tv)'

# How many redirects are followed, each one checked
MAX_REDIRECTS = 3

# Error codes – these are also key names in CONTENT.md
BAD_URL = 'ascii_bad_url'
TOO_BIG = 'ascii_too_big'
FAILED = 'ascii_failed'


class PictureError(Exception):
    """A refusal carrying a ready text key for chat."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def fetch(url: str) -> tuple[bytes, str]:
    """Download a picture. Returns (bytes, mime) or raises PictureError."""
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=Picture.TIMEOUT,
        headers={'User-Agent': USER_AGENT},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_url(url)
            result, url = await _get(client, url)
            if result is not None:
                return result
    logger.info('!ascii: слишком много переадресаций')
    raise PictureError(FAILED)


async def _get(client: httpx.AsyncClient, url: str) -> tuple[tuple[bytes, str] | None, str]:
    """One request. Either (data, mime) or the address of the next redirect."""
    try:
        async with client.stream('GET', url) as response:
            _check_peer(response)
            if response.is_redirect:
                location = response.headers.get('location')
                if not location:
                    raise PictureError(FAILED)
                # The address may be relative, but the absolute one is what must be checked
                return None, str(httpx.URL(url).join(location))
            if response.status_code != httpx.codes.OK:
                logger.info('!ascii: сервер ответил %s', response.status_code)
                raise PictureError(FAILED)

            mime = (response.headers.get('content-type') or '').split(';')[0].strip().lower()
            if not mime.startswith('image/'):
                logger.info('!ascii: по ссылке не картинка, а %r', mime)
                raise PictureError(FAILED)
            # The header is trusted only to avoid starting a download that is surely
            # too big: it may be missing or may lie, so the bytes are counted as well
            declared = response.headers.get('content-length', '')
            if declared.isdigit() and int(declared) > Picture.MAX_BYTES:
                raise PictureError(TOO_BIG)

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > Picture.MAX_BYTES:
                    raise PictureError(TOO_BIG)
                chunks.append(chunk)
            return (b''.join(chunks), mime), url
    except httpx.HTTPError as e:
        logger.info('!ascii: не скачалось (%s): %s', type(e).__name__, e)
        raise PictureError(FAILED) from None


def _check_peer(response: httpx.Response) -> None:
    """Check the address the connection was actually established with.

    Checking the name alone is not enough: httpx resolves it a second time, and a
    zero-TTL record can hand out a public address for the check and a local one for the
    download (DNS rebinding). The established connection has nothing left to swap.
    """
    stream = response.extensions.get('network_stream')
    if stream is None:
        return
    peer = stream.get_extra_info('server_addr')
    if not peer:
        return
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError:
        return
    if not address.is_global:
        logger.warning('!ascii: соединение ушло на непубличный адрес %s', address)
        raise PictureError(BAD_URL)


async def _check_url(url: str) -> None:
    """Scheme, host and all of its addresses. Raises PictureError."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise PictureError(BAD_URL)
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise PictureError(BAD_URL) from None
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        raise PictureError(BAD_URL) from None
    # Check every address: a host may resolve to both a public and a local
    # one – a single public address is not enough to consider the link safe
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise PictureError(BAD_URL) from None
        if not address.is_global:
            logger.warning('!ascii: ссылка ведёт на непубличный адрес %s', address)
            raise PictureError(BAD_URL)
