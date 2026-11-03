"""!кино without the hall's site: the gate is a stub, the answers and who may open or close a session."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core.config import Follow, Kino
from src.twitch.core.commands import CommandContext, Kind
from src.twitch.core.component import ChatComponent
from src.twitch.local import kino


@pytest.fixture
def hall(monkeypatch):
    """The command switched on and the gate replaced: `hall.state` is what GET answers."""
    monkeypatch.setattr(Kino, 'URL', 'https://kino.example')
    monkeypatch.setattr(Kino, 'API_TOKEN', 'token')
    monkeypatch.setattr(Kino, 'ENABLED', True)
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    hall = SimpleNamespace(calls=[], error=False, state={'active': True, 'code': '4817'})

    async def gate(method):
        hall.calls.append(method)
        if hall.error:
            raise kino.GateError('down')
        if method == 'POST':
            return {'active': True, 'code': '4817'}
        if method == 'DELETE':
            return {'active': False}
        return hall.state

    monkeypatch.setattr(kino, 'gate', gate)
    return hall


def _ctx(text: str, chatter=None, bot=None) -> CommandContext:
    message = make_message(text, chatter or make_chatter('gop'))
    return CommandContext(message=message, user=message.chatter.name, prompt=text.lower(),
                          original_text=text, session_id='2026-09-22 20:00', bot=bot or FakeBot(),
                          kind=Kind.LOCAL, args='')


async def test_an_open_session_gives_the_link_and_the_code(hall):
    ctx = _ctx('!кино')
    await kino.handle_kino(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.kino_info')
    assert hall.calls == ['GET']


async def test_no_session_is_closed(hall):
    hall.state = {'active': False}
    ctx = _ctx('!кино')
    await kino.handle_kino(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.kino_closed')


@pytest.mark.parametrize('handler', [kino.handle_kino, kino.handle_kino_start, kino.handle_kino_stop])
async def test_a_gate_that_does_not_answer_is_said_so(hall, handler):
    hall.error = True
    ctx = _ctx('!кино')
    await handler(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.kino_error')
    ctx.bot.send_chat_message.assert_not_awaited()


async def test_start_and_stop_are_announced_by_the_bot(hall):
    bot = FakeBot()
    await kino.handle_kino_start(_ctx('!кино старт', bot=bot))
    await kino.handle_kino_stop(_ctx('!кино стоп', bot=bot))
    assert hall.calls == ['POST', 'DELETE']
    assert [c.args[0] for c in bot.send_chat_message.await_args_list] == ['texts.kino_open', 'texts.kino_close']


async def test_the_broadcaster_opens_and_closes_through_chat(db, hall):
    bot = FakeBot()
    component = ChatComponent(bot)
    streamer = make_chatter('exitfound', broadcaster=True)
    await component.event_message(make_message('!кино старт', streamer))
    await component.event_message(make_message('!Кино Стоп', streamer))
    assert hall.calls == ['POST', 'DELETE']


@pytest.mark.parametrize('text', ['!кино старт', '!кино стоп'])
async def test_anyone_else_cannot_open_or_close(db, hall, text):
    bot = FakeBot()
    component = ChatComponent(bot)
    message = make_message(text, make_chatter('gop', moderator=True))
    await component.event_message(message)
    assert hall.calls == []
    message.respond.assert_not_awaited()
    bot.send_chat_message.assert_not_awaited()


async def test_any_viewer_gets_the_code_without_a_follow(db, hall, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    bot = FakeBot()
    bot.follows = False
    component = ChatComponent(bot)
    message = make_message('!кино', make_chatter('newcomer'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.kino_info')


async def test_without_the_site_there_is_no_command(db, monkeypatch):
    monkeypatch.setattr(Kino, 'ENABLED', False)
    gate = AsyncMock()
    monkeypatch.setattr(kino, 'gate', gate)
    component = ChatComponent(FakeBot())
    await component.event_message(make_message('!кино', make_chatter('gop')))
    gate.assert_not_awaited()


# --- the gate's HTTP call against a local server -----------------------------

@pytest.fixture
async def site(monkeypatch, unused_tcp_port):
    """A stand-in for the hall's gate on localhost: checks the token, answers like the real one."""
    from aiohttp import web

    seen = []

    async def session(request):
        seen.append((request.method, request.headers.get('Authorization')))
        if request.headers.get('Authorization') != 'Bearer token':
            return web.Response(status=401)
        return web.json_response({'active': request.method != 'DELETE', 'code': '0910'})

    app = web.Application()
    app.router.add_route('*', kino.API_PATH, session)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
    monkeypatch.setattr(Kino, 'URL', f'http://127.0.0.1:{unused_tcp_port}')
    monkeypatch.setattr(Kino, 'API_TOKEN', 'token')
    yield seen
    await runner.cleanup()


async def test_the_gate_is_asked_with_the_token(site):
    assert await kino.gate('POST') == {'active': True, 'code': '0910'}
    assert await kino.gate('DELETE') == {'active': False, 'code': '0910'}
    assert site == [('POST', 'Bearer token'), ('DELETE', 'Bearer token')]


async def test_a_refused_token_is_a_gate_error(site, monkeypatch):
    monkeypatch.setattr(Kino, 'API_TOKEN', 'wrong')
    with pytest.raises(kino.GateError, match='401'):
        await kino.gate('GET')


async def test_an_unreachable_gate_is_a_gate_error(monkeypatch, unused_tcp_port):
    monkeypatch.setattr(Kino, 'URL', f'http://127.0.0.1:{unused_tcp_port}')
    with pytest.raises(kino.GateError):
        await kino.gate('GET')
