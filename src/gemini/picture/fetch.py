"""Скачивание картинки по ссылке из чата.

Ссылку даёт случайный зритель, поэтому проверок больше, чем кода. Главное,
от чего защищаемся: ссылка на внутренний адрес превратила бы бота в способ
стучаться по локальной сети хозяина (SSRF). Отсюда явная проверка каждого
IP, в который резолвится хост, и ручной обход редиректов – автоматический
увёл бы нас на приватный адрес уже после проверки. Плюс проверка адреса
установленного соединения: имя резолвится второй раз внутри httpx, и запись
с нулевым TTL иначе подменила бы адрес между проверкой и запросом.

Заодно: только http/https, только image/*, таймаут и потолок размера.
"""
import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from src.core.config import Picture

logger = logging.getLogger(__name__)

# Представляемся: без User-Agent часть хостингов (Wikimedia, Reddit) даёт 403
USER_AGENT = 'sosuryan-twitch-bot/1.0 (+https://twitch.tv)'

# Сколько переадресаций проходим, проверяя каждую
MAX_REDIRECTS = 3

# Коды ошибок – это же имена ключей в CONTENT.md
BAD_URL = 'ascii_bad_url'
TOO_BIG = 'ascii_too_big'
FAILED = 'ascii_failed'


class PictureError(Exception):
    """Отказ с готовым ключом текста для чата."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def fetch(url: str) -> tuple[bytes, str]:
    """Скачать картинку. Возвращает (байты, mime) или бросает PictureError."""
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
    """Один запрос. Либо (данные, mime), либо адрес следующей переадресации."""
    try:
        async with client.stream('GET', url) as response:
            _check_peer(response)
            if response.is_redirect:
                location = response.headers.get('location')
                if not location:
                    raise PictureError(FAILED)
                # Адрес может быть относительным, а проверять надо абсолютный
                return None, str(httpx.URL(url).join(location))
            if response.status_code != httpx.codes.OK:
                logger.info('!ascii: сервер ответил %s', response.status_code)
                raise PictureError(FAILED)

            mime = (response.headers.get('content-type') or '').split(';')[0].strip().lower()
            if not mime.startswith('image/'):
                logger.info('!ascii: по ссылке не картинка, а %r', mime)
                raise PictureError(FAILED)
            # Заголовку верим только чтобы не начинать качать заведомо лишнее:
            # его может не быть или он может врать, поэтому считаем и байты
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
    """Проверить адрес, с которым соединение установлено на самом деле.

    Проверки одного только имени мало: между нашим запросом к DNS и запросом
    httpx имя резолвится второй раз, и запись с нулевым TTL может отдать
    публичный адрес на проверку и локальный на скачивание (DNS rebinding).
    Здесь мы смотрим уже на установленное соединение, поэтому подменить
    нечего.
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
    """Схема, хост и все его адреса. Бросает PictureError."""
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
    # Проверяем каждый адрес: хост может резолвиться и в публичный, и в
    # локальный – одного публичного мало, чтобы считать ссылку безопасной
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise PictureError(BAD_URL) from None
        if not address.is_global:
            logger.warning('!ascii: ссылка ведёт на непубличный адрес %s', address)
            raise PictureError(BAD_URL)
