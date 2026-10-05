"""Downloading a picture from a link in chat.

The link comes from a random viewer, so there are more checks than code. A link to an
internal address would turn the bot into a way of knocking on the host's local network
(SSRF). The name is resolved once, inside the connection, and the socket is opened to
an address that has just been checked: a check before the request would let httpx
resolve the name again (a zero-TTL record swaps it in between) or encode it otherwise
(IDNA 2003 vs 2008), and a check after it would come when the request is already sent.
Redirects are followed by hand, each through the same connection.

Also: http/https only, image/* only, a timeout and a size cap.
"""
import asyncio
import ipaddress
import logging
import socket

import httpcore
import httpx

from src.core.config import Picture

logger = logging.getLogger(__name__)

# Without a User-Agent some hosts (Wikimedia, Reddit) return 403
USER_AGENT = 'sosuryan-twitch-bot/1.0 (+https://twitch.tv)'

# How many redirects are followed, each one checked
MAX_REDIRECTS = 3

# The whole download, redirects included, in units of PICTURE_TIMEOUT
DEADLINE_FACTOR = 2

# NAT64 addresses carry an IPv4 one in the low 32 bits
NAT64 = ipaddress.ip_network('64:ff9b::/96')

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
    """Download a picture. Returns (bytes, mime) or raises PictureError.

    httpx's timeout applies to each connect and read separately, so a server dripping
    a byte at a time never trips it: the whole download, redirects included, gets a
    deadline of its own. The environment's proxy settings are ignored – through a proxy
    the address check would see the proxy, not the host – and the body is asked for
    uncompressed, since a compressed one is decoded past the size cap chunk by chunk.
    """
    try:
        async with asyncio.timeout(Picture.TIMEOUT * DEADLINE_FACTOR):
            async with httpx.AsyncClient(
                transport=_transport(),
                follow_redirects=False,
                timeout=Picture.TIMEOUT,
                trust_env=False,
                headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'},
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    _check_url(url)
                    result, url = await _get(client, url)
                    if result is not None:
                        return result
    except TimeoutError:
        logger.info('!ascii: скачивание не уложилось в %s с', Picture.TIMEOUT * DEADLINE_FACTOR)
        raise PictureError(FAILED) from None
    logger.info('!ascii: слишком много переадресаций')
    raise PictureError(FAILED)


async def _get(client: httpx.AsyncClient, url: str) -> tuple[tuple[bytes, str] | None, str]:
    """One request. Either (data, mime) or the address of the next redirect."""
    try:
        async with client.stream('GET', url) as response:
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
    except httpx.InvalidURL:
        raise PictureError(BAD_URL) from None
    except httpx.HTTPError as e:
        logger.info('!ascii: не скачалось (%s): %s', type(e).__name__, e)
        raise PictureError(FAILED) from None


def _check_url(url: str) -> None:
    """Scheme, host and port, parsed the way httpx will parse them. Raises PictureError."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise PictureError(BAD_URL) from None
    if parsed.scheme not in ('http', 'https') or not parsed.host:
        raise PictureError(BAD_URL)
    # httpx accepts any number here and fails only when it connects
    if parsed.port is not None and not 0 < parsed.port < 65536:
        raise PictureError(BAD_URL)


def _transport() -> httpx.AsyncHTTPTransport:
    """httpx's transport with a connection pool that connects through _PublicOnly.

    httpx takes no network backend of its own, so the pool it built is replaced before
    any connection exists; test_picture.py checks the swap still reaches the socket.
    """
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        network_backend=_PublicOnly(),
    )
    return transport


class _PublicOnly(httpcore.AsyncNetworkBackend):
    """Resolves the host itself and opens the socket only to an address it checked.

    TLS verifies the certificate against the name, not the address: httpcore passes the
    host from the URL to the handshake.
    """

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    # httpcore's own signature: its pool passes the connect timeout by this name
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109
        error: Exception | None = None
        for address in await _public_addresses(host, port):
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                error = e
        raise error or httpcore.ConnectError(f'{host}: нет адресов')

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


async def _public_addresses(host: str, port: int) -> list[str]:
    """Every address the host resolves to, or PictureError if any of them is not public.

    A host that resolves to both a public and a local address is refused: which one the
    connection takes is not up to the bot.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        )
    except (socket.gaierror, UnicodeError, OSError):
        raise PictureError(BAD_URL) from None
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise PictureError(BAD_URL) from None
        if not _is_public(address):
            logger.warning('!ascii: ссылка ведёт на непубличный адрес %s', address)
            raise PictureError(BAD_URL)
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise PictureError(BAD_URL)
    return addresses


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A global address. An IPv6 one that carries an IPv4 address (mapped, 6to4, NAT64,
    the old compatible form) counts only if that one is global too: ipaddress calls
    64:ff9b::7f00:1 global, yet a NAT64 gateway delivers it to 127.0.0.1."""
    if not address.is_global:
        return False
    if address.version == 6:
        inner = address.ipv4_mapped or address.sixtofour
        if inner is None and (address in NAT64 or int(address) < 2 ** 32):
            inner = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if inner is not None and not inner.is_global:
            return False
    return True
