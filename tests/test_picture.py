"""!ascii without the network: the SSRF guard on numeric addresses and the renderer on
pictures built in memory."""
import asyncio
import io
import ipaddress
import pytest
from google.genai import types
from PIL import Image, ImageDraw

from fakes import FakeBot, make_chatter, make_message
from src.core.commands import CommandContext, Kind
from src.core.config import Gemini
from src.core.database import count_bot_uses
from src.core import limits
from src.gemini.picture import command, fetch, render
from src.gemini.picture.fetch import BAD_URL, PictureError

BRAILLE = {chr(c) for c in range(0x2800, 0x2900)}


# --- SSRF -------------------------------------------------------------------
# Numeric hosts and localhost resolve without DNS, so these run offline

@pytest.mark.parametrize('url', [
    'http://127.0.0.1/a.png',
    'http://localhost/a.png',
    'http://10.0.0.1/a.png',
    'http://192.168.1.1/a.png',
    'http://169.254.169.254/latest/meta-data',
    'http://[::1]/a.png',
    'http://[::ffff:127.0.0.1]/a.png',
    'http://0.0.0.0/a.png',
    'http://93.184.216.34:99999/a.png',
    'file:///etc/passwd',
    'ftp://93.184.216.34/a.png',
    'http:///a.png',
])
async def test_private_or_malformed_urls_are_refused(url):
    with pytest.raises(PictureError) as error:
        await fetch.fetch(url)
    assert error.value.code == BAD_URL


@pytest.mark.parametrize('address', [
    '::ffff:127.0.0.1',     # IPv4-mapped
    '64:ff9b::7f00:1',      # NAT64 – ipaddress itself calls it global
    '2002:7f00:1::',        # 6to4
    '::127.0.0.1',          # IPv4-compatible
])
def test_ipv6_carrying_a_local_ipv4_is_not_public(address):
    assert not fetch._is_public(ipaddress.ip_address(address))


def test_public_addresses_pass():
    for address in ('93.184.216.34', '2606:4700::1111', '64:ff9b::808:808'):
        assert fetch._is_public(ipaddress.ip_address(address))


class _Server:
    """A local HTTP server answering from a table of raw responses; records every
    request line it receives."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        head = await reader.readuntil(b'\r\n\r\n')
        path = head.split(b' ')[1].decode()
        self.requests.append(path)
        writer.write(self.routes.get(path, b'HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n'))
        await writer.drain()
        writer.close()


def _ok(body: bytes, mime: str = 'image/png', length: bool = True) -> bytes:
    size = f'content-length: {len(body)}\r\n' if length else ''
    return f'HTTP/1.1 200 OK\r\ncontent-type: {mime}\r\n{size}connection: close\r\n\r\n'.encode() + body


def _redirect(location: str) -> bytes:
    return f'HTTP/1.1 302 Found\r\nlocation: {location}\r\ncontent-length: 0\r\n\r\n'.encode()


@pytest.fixture
def loopback_is_public(monkeypatch):
    """Let 127.0.0.1 through the address check, so a local server can stand in for the
    internet; every other non-public address stays refused."""
    real = fetch._is_public
    monkeypatch.setattr(fetch, '_is_public', lambda a: str(a) == '127.0.0.1' or real(a))


async def test_a_local_address_gets_no_connection_at_all():
    """The check happens before the socket opens: a request sent first and judged by
    its peer afterwards would already have reached the internal service."""
    async with _Server({'/a.png': _ok(b'png')}) as server:
        with pytest.raises(PictureError) as error:
            await fetch.fetch(f'http://localhost:{server.port}/a.png')
    assert error.value.code == BAD_URL
    assert server.requests == []


async def test_download_goes_through_the_checked_connection(loopback_is_public):
    """The pool swapped into httpx's transport is the one that reaches the socket."""
    async with _Server({'/a': _redirect('/b.png'), '/b.png': _ok(b'png')}) as server:
        assert await fetch.fetch(f'http://127.0.0.1:{server.port}/a') == (b'png', 'image/png')
    assert server.requests == ['/a', '/b.png']


async def test_a_redirect_to_a_local_address_is_refused(loopback_is_public):
    async with _Server({'/a.png': _redirect('http://127.0.0.2/b.png')}) as server:
        with pytest.raises(PictureError) as error:
            await fetch.fetch(f'http://127.0.0.1:{server.port}/a.png')
    assert error.value.code == BAD_URL
    assert server.requests == ['/a.png']


async def test_a_body_past_the_cap_is_cut_by_counted_bytes(loopback_is_public, monkeypatch):
    """No content-length to trust: the bytes themselves are counted."""
    monkeypatch.setattr(fetch.Picture, 'MAX_BYTES', 1000)
    async with _Server({'/a.png': _ok(b'x' * 5000, length=False)}) as server:
        with pytest.raises(PictureError) as error:
            await fetch.fetch(f'http://127.0.0.1:{server.port}/a.png')
    assert error.value.code == fetch.TOO_BIG


async def test_not_a_picture_is_refused(loopback_is_public):
    async with _Server({'/a.png': _ok(b'<html>', mime='text/html')}) as server:
        with pytest.raises(PictureError) as error:
            await fetch.fetch(f'http://127.0.0.1:{server.port}/a.png')
    assert error.value.code == fetch.FAILED


async def test_whole_download_has_a_deadline(monkeypatch):
    """httpx's timeout is per operation: a server dripping a byte every few seconds would
    otherwise hold the viewer's slot and a socket for as long as it likes."""
    def ok(url):
        pass

    async def drip(client, url):
        await asyncio.sleep(3600)
    monkeypatch.setattr(fetch, '_check_url', ok)
    monkeypatch.setattr(fetch, '_get', drip)
    monkeypatch.setattr(fetch.Picture, 'TIMEOUT', 0.05)
    # The test's own deadline: without the fix it would hang instead of failing
    async with asyncio.timeout(2):
        with pytest.raises(PictureError) as error:
            await fetch.fetch('https://93.184.216.34/slow.png')
    assert error.value.code == fetch.FAILED


async def test_client_ignores_proxy_env_and_compression(monkeypatch):
    """A proxy would make the address check look at the proxy; a compressed body would be
    decoded past the size cap one chunk at a time."""
    seen = {}
    real = fetch.httpx.AsyncClient

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    def ok(url):
        pass

    async def done(client, url):
        return (b'png', 'image/png'), url
    monkeypatch.setattr(fetch.httpx, 'AsyncClient', spy)
    monkeypatch.setattr(fetch, '_check_url', ok)
    monkeypatch.setattr(fetch, '_get', done)
    assert await fetch.fetch('https://93.184.216.34/a.png') == (b'png', 'image/png')
    assert seen['trust_env'] is False
    assert seen['headers']['Accept-Encoding'] == 'identity'


# --- rendering --------------------------------------------------------------

def _png(img: Image.Image, fmt: str = 'PNG') -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, fmt)
    return buffer.getvalue()


def _circle(mode: str, size=(200, 160)) -> Image.Image:
    img = Image.new('RGBA', size, (255, 255, 255, 0))
    ImageDraw.Draw(img).ellipse((30, 20, 170, 140), fill=(20, 120, 40, 255), outline=(0, 0, 0, 255), width=6)
    return img.convert(mode) if mode != 'RGBA' else img


@pytest.mark.parametrize(('mode', 'fmt'), [
    ('RGBA', 'PNG'), ('RGB', 'JPEG'), ('L', 'PNG'), ('LA', 'PNG'), ('CMYK', 'JPEG'), ('I;16', 'PNG'), ('P', 'PNG'),
])
def test_render_draws_one_message_of_braille(mode, fmt):
    art = render.render(_png(_circle(mode), fmt), limit=450, max_cols=26)
    assert art
    assert len(art) <= 450
    lines = art.split(' ')
    assert all(set(line) <= BRAILLE for line in lines)
    assert max(len(line) for line in lines) <= 26
    assert set(art) - {' ', render.BLANK}, 'the picture came out empty'


def test_render_refuses_a_decompression_bomb_before_decoding():
    """A small file with a huge header must be refused by its size, not after load()."""
    bomb = _png(Image.new('1', (8000, 6000)))
    assert len(bomb) < 100_000
    assert render.render(bomb, limit=450, max_cols=26) is None
    assert render.preview(bomb) is None


def test_render_refuses_garbage():
    assert render.render(b'not a picture', limit=450, max_cols=26) is None


def test_preview_is_a_small_jpeg():
    data, mime = render.preview(_png(_circle('RGBA', (2000, 1500))))
    assert mime == 'image/jpeg'
    small = Image.open(io.BytesIO(data))
    assert max(small.size) <= render.PREVIEW_PX


async def test_picture_check_goes_through_the_shared_config(monkeypatch):
    """A hand-built config drops thinking_config, and Gemini then thinks dynamically on
    every check – paid tokens and seconds for a yes/no verdict. The check has no persona
    and its own safety thresholds: here the classifier is a second layer of the veto."""
    sent = {}

    async def fake_generate(contents, config):
        sent['config'] = config
        return 'МОЖНО кружок'
    monkeypatch.setattr(command, 'generate', fake_generate)
    ctx = CommandContext(message=make_message('!ascii x'), user='gop', prompt='', original_text='',
                         session_id='s', bot=FakeBot(), kind=Kind.GEMINI)
    assert await command._look(_png(_circle('RGBA')), ctx) == 'МОЖНО кружок'

    config = sent['config']
    assert config.system_instruction is None
    assert config.thinking_config.thinking_budget == Gemini.THINKING_BUDGET
    thresholds = {s.category: s.threshold for s in config.safety_settings}
    blocked = types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
    assert thresholds[types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT] == blocked
    assert thresholds[types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT] == blocked


def test_big_picture_is_shrunk_before_any_conversion():
    """Every step after decoding works on a copy of at most WORK_PX: converting and
    compositing a full-size 40-megapixel picture took over half a gigabyte."""
    img = render._open(_png(_circle('RGBA', (3000, 2000))))
    assert max(img.size) == render.WORK_PX
    assert img.mode == 'RGBA'


def test_big_jpeg_is_decoded_at_a_reduced_scale():
    buffer = io.BytesIO()
    _circle('RGB', (4000, 3000)).save(buffer, 'JPEG')
    img = render._open(buffer.getvalue())
    assert max(img.size) <= render.WORK_PX


def _ascii_ctx(bot=None):
    text = '!ascii https://93.184.216.34/a.png'
    return CommandContext(message=make_message(text, make_chatter('gop', subscriber=True)), user='gop',
                          prompt=text, original_text=text, session_id='2026-09-22 20:00',
                          bot=bot or FakeBot(), kind=Kind.GEMINI)


async def test_unexpected_error_answers_the_viewer(db, monkeypatch):
    """An error outside the expected refusals went to twitchio's log, and the viewer who
    paid a quota slot got silence."""
    async def broken(url):
        raise RuntimeError('pillow exploded')
    monkeypatch.setattr(command, 'fetch', broken)
    ctx = _ascii_ctx()
    await command.handle_ascii(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.ascii_failed')
    assert 'gop' not in command.LIMIT.busy


async def test_art_that_did_not_reach_chat_is_reported_and_not_counted(db, monkeypatch):
    async def picture(url):
        return _png(_circle('RGBA')), 'image/png'
    monkeypatch.setattr(command, 'fetch', picture)
    monkeypatch.setattr(command.Picture, 'CHECK', False)
    bot = FakeBot()
    bot.send_chat_message.return_value = False
    ctx = _ascii_ctx(bot)
    await command.handle_ascii(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.ascii_failed')
    assert await count_bot_uses('gop', command.USE_KIND, 60) == 0


async def test_bookkeeping_error_after_the_art_is_not_reported_as_a_failure(db, monkeypatch):
    """The viewer already sees the picture: an error while recording it must not add
    «failed» under it."""
    async def picture(url):
        return _png(_circle('RGBA')), 'image/png'

    async def broken(*args):
        raise RuntimeError('db locked')
    monkeypatch.setattr(command, 'fetch', picture)
    monkeypatch.setattr(command.Picture, 'CHECK', False)
    monkeypatch.setattr(limits, 'record_bot_use', broken)
    ctx = _ascii_ctx()
    await command.handle_ascii(ctx)
    ctx.bot.send_chat_message.assert_awaited_once()
    ctx.message.respond.assert_not_awaited()
