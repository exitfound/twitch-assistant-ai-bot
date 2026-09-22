"""!ascii without the network: the SSRF guard on numeric addresses and the renderer on
pictures built in memory."""
import asyncio
import io
from types import SimpleNamespace

import pytest
from google.genai import types
from PIL import Image, ImageDraw

from fakes import FakeBot, make_chatter, make_message
from src.core.commands import KIND_GEMINI, CommandContext
from src.core.config import Gemini
from src.core.database import count_bot_uses
from src.gemini.picture import command, fetch, render
from src.gemini.picture.fetch import BAD_URL, PictureError

BRAILLE = {chr(c) for c in range(0x2800, 0x2900)}


# --- SSRF -------------------------------------------------------------------
# Numeric hosts resolve without DNS, so these run offline

@pytest.mark.parametrize('url', [
    'http://127.0.0.1/a.png',
    'http://localhost/a.png',
    'http://10.0.0.1/a.png',
    'http://192.168.1.1/a.png',
    'http://169.254.169.254/latest/meta-data',
    'http://[::1]/a.png',
    'http://0.0.0.0/a.png',
    'http://93.184.216.34:99999/a.png',
    'file:///etc/passwd',
    'ftp://93.184.216.34/a.png',
    'http:///a.png',
])
async def test_private_or_malformed_urls_are_refused(url):
    with pytest.raises(PictureError) as error:
        await fetch._check_url(url)
    assert error.value.code == BAD_URL


async def test_public_address_passes():
    await fetch._check_url('https://93.184.216.34/picture.png')


def _response(peer):
    stream = SimpleNamespace(get_extra_info=lambda key: peer)
    return SimpleNamespace(extensions={'network_stream': stream})


def test_connection_to_a_private_address_is_refused():
    """DNS rebinding: the name checked out, the connection went elsewhere."""
    with pytest.raises(PictureError):
        fetch._check_peer(_response(('10.0.0.5', 443)))


def test_connection_to_a_public_address_passes():
    fetch._check_peer(_response(('93.184.216.34', 443)))


@pytest.mark.parametrize('response', [
    SimpleNamespace(extensions={}),
    _response(None),
])
def test_unknown_peer_is_refused(response):
    """The last line against DNS rebinding must not be skipped when it cannot look."""
    with pytest.raises(PictureError):
        fetch._check_peer(response)


async def test_whole_download_has_a_deadline(monkeypatch):
    """httpx's timeout is per operation: a server dripping a byte every few seconds would
    otherwise hold the viewer's slot and a socket for as long as it likes."""
    async def ok(url):
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
    """A proxy would make the peer check look at the proxy; a compressed body would be
    decoded past the size cap one chunk at a time."""
    seen = {}
    real = fetch.httpx.AsyncClient

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    async def ok(url):
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
                         session_id='s', bot=FakeBot(), kind=KIND_GEMINI)
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
                          bot=bot or FakeBot(), kind=KIND_GEMINI)


async def test_unexpected_error_answers_the_viewer(db, monkeypatch):
    """An error outside the expected refusals went to twitchio's log, and the viewer who
    paid a quota slot got silence."""
    async def broken(url):
        raise RuntimeError('pillow exploded')
    monkeypatch.setattr(command, 'fetch', broken)
    ctx = _ascii_ctx()
    await command.handle_ascii(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.ascii_failed')
    assert 'gop' not in command._busy


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
    monkeypatch.setattr(command, 'record_bot_use', broken)
    ctx = _ascii_ctx()
    await command.handle_ascii(ctx)
    ctx.bot.send_chat_message.assert_awaited_once()
    ctx.message.respond.assert_not_awaited()
