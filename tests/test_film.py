"""!film without the hall's site: the gate is a stub, the answers, the title and who may use what."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fakes import FakeBot, make_chatter, make_message, chat_component
from src.core import config
from src.core.config import Film, Follow
from src.twitch.core.commands import CommandContext, Kind
from src.twitch.local import film


@pytest.fixture
def hall(monkeypatch):
    """The commands switched on and the gate replaced: `hall.state` is what GET answers."""
    monkeypatch.setattr(Film, 'URL', 'https://kino.example')
    monkeypatch.setattr(Film, 'API_TOKEN', 'token')
    monkeypatch.setattr(Film, 'ENABLED', True)
    monkeypatch.setattr(Film, 'INFO_ACCESS', frozenset({'follower', 'vip', 'sub', 'mod'}))
    monkeypatch.setattr(Film, 'CONTROL_ACCESS', frozenset({'broadcaster'}))
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    hall = SimpleNamespace(calls=[], error=None, state={'active': True, 'code': '4817', 'title': None})

    async def gate(method, title=None, limit=None):
        hall.calls.append((method, title))
        if hall.error:
            raise hall.error
        if method == 'POST':
            hall.state = {'active': True, 'code': '4817', 'title': title}
        elif method == 'PATCH':
            hall.state = {**hall.state, 'title': title}
        elif method == 'DELETE':
            closed = bool(hall.state.get('active'))
            hall.state = {'active': False}
            return {**hall.state, 'closed': closed}
        return hall.state

    monkeypatch.setattr(film, 'gate', gate)
    return hall


def _ctx(text: str, args: str = '', chatter=None, bot=None) -> CommandContext:
    message = make_message(text, chatter or make_chatter('gop'))
    return CommandContext(message=message, user=message.chatter.name, prompt=text.lower(),
                          original_text=text, session_id='2026-09-22 20:00', bot=bot or FakeBot(),
                          kind=Kind.LOCAL, args=args.lower())


def _said(bot: FakeBot) -> list[str]:
    return [c.args[0] for c in bot.send_chat_message.await_args_list]


# --- the handlers ---------------------------------------------------------------

async def test_an_open_session_gives_the_link_and_the_code(hall):
    ctx = _ctx('!film')
    await film.handle_film(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.film_info')
    assert hall.calls == [('GET', None)]


async def test_a_titled_session_names_the_film(hall):
    hall.state['title'] = 'Чужие'
    ctx = _ctx('!film')
    await film.handle_film(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.film_info_titled')


async def test_no_session_is_closed(hall):
    hall.state = {'active': False}
    ctx = _ctx('!film')
    await film.handle_film(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.film_closed')


@pytest.mark.parametrize('handler', [film.handle_film, film.handle_film_start, film.handle_film_stop])
async def test_a_gate_that_does_not_answer_is_said_so(hall, handler):
    hall.error = film.GateError('down')
    ctx = _ctx('!film')
    await handler(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.film_error')
    ctx.bot.send_chat_message.assert_not_awaited()


async def test_start_and_stop_are_announced_by_the_bot(hall):
    hall.state = {'active': False}
    bot = FakeBot()
    await film.handle_film_start(_ctx('!film start', bot=bot))
    await film.handle_film_stop(_ctx('!film stop', bot=bot))
    assert hall.calls == [('GET', None), ('POST', None), ('DELETE', None)]
    assert _said(bot) == ['texts.film_open', 'texts.film_close']


async def test_start_with_a_title_keeps_its_case_and_announces_it(hall):
    hall.state = {'active': False}
    bot = FakeBot()
    await film.handle_film_start(_ctx('!film start  Бегущий   по Лезвию', 'Бегущий   по Лезвию', bot=bot))
    assert hall.calls == [('GET', None), ('POST', 'Бегущий по Лезвию')]
    assert _said(bot) == ['texts.film_open_titled']


async def test_a_long_title_is_cut(hall):
    hall.state = {'active': False}
    await film.handle_film_start(_ctx('!film start ' + 'я' * 300, 'я' * 300))
    assert hall.calls[-1] == ('POST', 'я' * film.TITLE_MAX)


async def test_start_while_a_session_is_open_keeps_it_and_says_how(hall):
    """A new session would throw every viewer back to the code page mid-film."""
    bot = FakeBot()
    ctx = _ctx('!film start Чужие 2', 'Чужие 2', bot=bot)
    bot.set_cooldown('gop', 30, ctx.kind)
    await film.handle_film_start(ctx)
    assert hall.calls == [('GET', None)]
    assert hall.state['code'] == '4817'
    ctx.message.respond.assert_awaited_once_with('texts.film_already_open')
    bot.send_chat_message.assert_not_awaited()
    assert not bot.cooldown_remaining('gop', ctx.kind)


async def test_stop_with_nothing_open_does_not_announce_an_end(hall):
    hall.state = {'active': False}
    bot = FakeBot()
    ctx = _ctx('!film stop', bot=bot)
    await film.handle_film_stop(ctx)
    assert hall.calls == [('DELETE', None)]
    ctx.message.respond.assert_awaited_once_with('texts.film_stop_none')
    bot.send_chat_message.assert_not_awaited()


async def test_title_renames_the_open_session(hall):
    bot = FakeBot()
    await film.handle_film_title(_ctx('!film title Чужие', 'Чужие', bot=bot))
    assert hall.calls == [('PATCH', 'Чужие')]
    assert _said(bot) == ['texts.film_retitled']


async def test_title_without_a_name_shows_the_usage_and_gives_the_pause_back(hall):
    ctx = _ctx('!film title')
    ctx.bot.set_cooldown('gop', 30, ctx.kind)
    await film.handle_film_title(ctx)
    assert hall.calls == []
    ctx.message.respond.assert_awaited_once_with('texts.film_title_usage')
    assert not ctx.bot.cooldown_remaining('gop', ctx.kind)


async def test_title_without_a_session_is_closed(hall):
    hall.error = film.NoSession('PATCH')
    ctx = _ctx('!film title Чужие', 'Чужие')
    await film.handle_film_title(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.film_closed')
    ctx.bot.send_chat_message.assert_not_awaited()


# --- through the dispatcher: routing and roles ----------------------------------

async def test_the_broadcaster_controls_through_chat(db, hall):
    hall.state = {'active': False}
    component = chat_component(FakeBot())
    streamer = make_chatter('exitfound', broadcaster=True)
    await component.event_message(make_message('!film start Чужие', streamer))
    await component.event_message(make_message('!Film Title Чужие 2', streamer))
    await component.event_message(make_message('!FILM STOP', streamer))
    assert hall.calls == [('GET', None), ('POST', 'Чужие'), ('PATCH', 'Чужие 2'), ('DELETE', None)]


async def test_a_usage_slip_does_not_hold_up_the_right_command(db, hall, monkeypatch):
    """A VIP with control rights: «!film title» without a name, then with one, at once."""
    monkeypatch.setattr(Film, 'CONTROL_ACCESS', frozenset({'vip'}))
    component = chat_component(FakeBot())
    vip = make_chatter('vipper', vip=True)
    slip, fixed = make_message('!film title', vip), make_message('!film title Чужие', vip)
    await component.event_message(slip)
    await component.event_message(fixed)
    slip.respond.assert_awaited_once_with('texts.film_title_usage')
    assert hall.calls == [('PATCH', 'Чужие')]


@pytest.mark.parametrize('text', ['!film start', '!film start Чужие', '!film title Чужие', '!film stop'])
async def test_anyone_else_is_told_it_is_not_their_level(db, hall, text):
    bot = FakeBot()
    component = chat_component(bot)
    message = make_message(text, make_chatter('gop', moderator=True, subscriber=True, vip=True))
    await component.event_message(message)
    assert hall.calls == []
    message.respond.assert_awaited_once_with('texts.film_no_rights')
    bot.send_chat_message.assert_not_awaited()


async def test_the_refusal_is_not_repeated_within_its_pause(db, hall):
    component = chat_component(FakeBot())
    gop = make_chatter('gop')
    first, second = make_message('!film start', gop), make_message('!film stop', gop)
    await component.event_message(first)
    await component.event_message(second)
    first.respond.assert_awaited_once_with('texts.film_no_rights')
    second.respond.assert_not_awaited()
    assert hall.calls == []


async def test_control_can_be_given_to_moderators(db, hall, monkeypatch):
    monkeypatch.setattr(Film, 'CONTROL_ACCESS', frozenset({'mod'}))
    component = chat_component(FakeBot())
    await component.event_message(make_message('!film stop', make_chatter('modder', moderator=True)))
    assert hall.calls == [('DELETE', None)]


@pytest.mark.parametrize('badge', ['vip', 'subscriber', 'founder', 'moderator'])
async def test_badged_viewers_get_the_code_without_a_follow(db, hall, badge):
    bot = FakeBot()
    bot.follows = False
    component = chat_component(bot)
    message = make_message('!film', make_chatter('badged', **{badge: True}))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.film_info')


async def test_a_follower_gets_the_code(db, hall):
    component = chat_component(FakeBot())
    message = make_message('!film', make_chatter('fan'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.film_info')


@pytest.mark.parametrize('required', [True, False])
async def test_a_non_follower_gets_the_follow_hint(db, hall, monkeypatch, required):
    monkeypatch.setattr(Follow, 'REQUIRED', required)
    bot = FakeBot()
    bot.follows = False
    component = chat_component(bot)
    message = make_message('!film', make_chatter('newcomer'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.follow_required')
    assert hall.calls == []


async def test_all_lets_in_a_non_follower(db, hall, monkeypatch):
    monkeypatch.setattr(Film, 'INFO_ACCESS', frozenset({'all'}))
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    bot = FakeBot()
    bot.follows = False
    component = chat_component(bot)
    message = make_message('!film', make_chatter('newcomer'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.film_info')


async def test_a_follower_outside_the_roles_is_dropped(db, hall, monkeypatch):
    monkeypatch.setattr(Film, 'INFO_ACCESS', frozenset({'sub'}))
    component = chat_component(FakeBot())
    message = make_message('!film', make_chatter('fan'))
    await component.event_message(message)
    message.respond.assert_not_awaited()
    assert hall.calls == []


async def test_without_the_site_there_is_no_command(db, monkeypatch):
    monkeypatch.setattr(Film, 'ENABLED', False)
    gate = AsyncMock()
    monkeypatch.setattr(film, 'gate', gate)
    component = chat_component(FakeBot())
    await component.event_message(make_message('!film', make_chatter('gop')))
    gate.assert_not_awaited()


# --- the roles from .env --------------------------------------------------------

def test_roles_are_read_and_unknown_ones_skipped(monkeypatch, caplog):
    monkeypatch.setenv('FILM_INFO_ACCESS', ' VIP, sub ,friends,,')
    with caplog.at_level(logging.WARNING):
        assert config._env_roles('FILM_INFO_ACCESS', 'follower') == {'vip', 'sub'}
    assert 'friends' in caplog.text


def test_no_valid_role_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv('FILM_INFO_ACCESS', 'friends')
    assert config._env_roles('FILM_INFO_ACCESS', 'follower,vip') == {'follower', 'vip'}
    monkeypatch.delenv('FILM_INFO_ACCESS')
    assert config._env_roles('FILM_INFO_ACCESS', 'broadcaster') == {'broadcaster'}


# --- the gate's HTTP call against a local server --------------------------------

@pytest.fixture
async def site(monkeypatch, unused_tcp_port):
    """A stand-in for the hall's gate on localhost: checks the token, answers like the real one."""
    from aiohttp import web

    seen = []
    state = {'session': None}

    async def session(request):
        body = await request.json() if request.can_read_body else None
        seen.append((request.method, request.headers.get('Authorization'), body))
        if request.headers.get('Authorization') != 'Bearer token':
            return web.Response(status=401)
        if request.method == 'PATCH' and state['session'] is None:
            return web.json_response({'active': False}, status=409)
        if request.method == 'POST':
            state['session'] = {'active': True, 'code': '0910', 'title': body['title']}
        elif request.method == 'PATCH':
            state['session'] = {**state['session'], 'title': body['title']}
        elif request.method == 'DELETE':
            state['session'] = None
        return web.json_response(state['session'] or {'active': False})

    app = web.Application()
    app.router.add_route('*', film.API_PATH, session)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
    monkeypatch.setattr(Film, 'URL', f'http://127.0.0.1:{unused_tcp_port}')
    monkeypatch.setattr(Film, 'API_TOKEN', 'token')
    yield seen
    await runner.cleanup()


async def test_the_gate_is_asked_with_the_token_and_the_title(site):
    with pytest.raises(film.NoSession):
        await film.gate('PATCH', 'Чужие')
    assert await film.gate('POST', 'Чужие') == {'active': True, 'code': '0910', 'title': 'Чужие'}
    assert (await film.gate('PATCH', 'Чужие 2'))['title'] == 'Чужие 2'
    assert await film.gate('DELETE') == {'active': False}
    assert site == [('PATCH', 'Bearer token', {'title': 'Чужие'}),
                    ('POST', 'Bearer token', {'title': 'Чужие'}),
                    ('PATCH', 'Bearer token', {'title': 'Чужие 2'}),
                    ('DELETE', 'Bearer token', None)]


async def test_a_refused_token_is_a_gate_error(site, monkeypatch):
    monkeypatch.setattr(Film, 'API_TOKEN', 'wrong')
    with pytest.raises(film.GateError, match='401'):
        await film.gate('GET')


async def test_an_unreachable_gate_is_a_gate_error(monkeypatch, unused_tcp_port):
    monkeypatch.setattr(Film, 'URL', f'http://127.0.0.1:{unused_tcp_port}')
    with pytest.raises(film.GateError):
        await film.gate('GET')


# --- the session left open at the stream end ------------------------------------

@pytest.fixture
def ended(hall, monkeypatch):
    """The stream off, a session open at its end; `ended.during_wait` runs while the window is waited out."""
    from src.core.config import Stream
    monkeypatch.setattr(Stream, 'RESUME_MINUTES', 15)
    hall.state = {'active': True, 'code': '4817', 'started': 1000, 'title': None}
    bot = FakeBot(stream_live=False)
    bot.during_wait = lambda: None
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        bot.during_wait()
    monkeypatch.setattr(film.asyncio, 'sleep', sleep)
    bot.waits = waits
    return bot


async def test_a_session_left_open_is_closed_after_the_stream(hall, ended):
    await film.close_after_stream(ended)
    assert ended.waits == [15 * 60]
    assert hall.calls == [('GET', None), ('GET', None), ('DELETE', None)]
    assert _said(ended) == ['texts.film_auto_close']


async def test_a_renamed_session_is_still_the_one_left_open(hall, ended):
    ended.during_wait = lambda: hall.state.update(title='Чужие')
    await film.close_after_stream(ended)
    assert hall.calls[-1] == ('DELETE', None)


async def test_a_stream_back_within_the_window_keeps_the_session(hall, ended):
    def back():
        ended.stream_live = True
    ended.during_wait = back
    await film.close_after_stream(ended)
    assert hall.calls == [('GET', None)]


async def test_a_film_started_off_stream_is_left_alone(hall, ended):
    """Closed and opened again after the end: a different session, whatever the clocks say."""
    def new_session():
        hall.state = {'active': True, 'code': '5120', 'started': 990, 'title': 'Чужие'}
    ended.during_wait = new_session
    await film.close_after_stream(ended)
    assert hall.calls == [('GET', None), ('GET', None)]
    ended.send_chat_message.assert_not_awaited()


async def test_no_session_at_the_end_nothing_to_wait_for(hall, ended):
    hall.state = {'active': False}
    await film.close_after_stream(ended)
    assert hall.calls == [('GET', None)]
    assert ended.waits == []
    ended.send_chat_message.assert_not_awaited()


async def test_a_session_closed_during_the_wait_is_not_announced(hall, ended):
    ended.during_wait = lambda: hall.state.update(active=False)
    await film.close_after_stream(ended)
    assert hall.calls == [('GET', None), ('GET', None)]
    ended.send_chat_message.assert_not_awaited()


@pytest.mark.parametrize('when', ['at the end', 'after the wait'])
async def test_a_gate_that_does_not_answer_at_the_end_is_only_logged(hall, ended, when):
    def down():
        hall.error = film.GateError('down')
    if when == 'at the end':
        down()
    else:
        ended.during_wait = down
    await film.close_after_stream(ended)
    ended.send_chat_message.assert_not_awaited()
    assert ('DELETE', None) not in hall.calls


async def test_a_waiting_close_is_cancelled_by_name():
    import asyncio

    from src.core.tasks import BackgroundTasks
    tasks = BackgroundTasks()
    tasks.start('film_close', lambda: asyncio.sleep(3600), once=True)
    assert tasks.cancel('film_close') is True
    await asyncio.sleep(0)
    assert not tasks.running('film_close')
    assert tasks.cancel('film_close') is False


# --- the session lives only while the bot does -----------------------------------

async def test_the_heartbeat_asks_the_gate_every_minute(hall, monkeypatch):
    import asyncio
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(film.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await film.heartbeat_loop(FakeBot())
    assert hall.calls == [('GET', None)] * 3
    assert sleeps == [film.HEARTBEAT_SECONDS] * 3


async def test_a_failing_heartbeat_warns_once_and_keeps_going(hall, monkeypatch, caplog):
    import asyncio
    hall.error = film.GateError('down')
    rounds = []

    async def sleep(_seconds):
        rounds.append(1)
        if len(rounds) == 2:
            hall.error = None
        if len(rounds) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(film.asyncio, 'sleep', sleep)
    with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
        await film.heartbeat_loop(FakeBot())
    assert caplog.text.count('не отвечает на отметку') == 1
    assert 'снова отвечает' in caplog.text


# --- the reminder: the link and the code again while a session is open -----------------

@pytest.fixture
def clock(monkeypatch):
    """The bot's monotonic clock under the test's hand: `clock.now` in seconds."""
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(film.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(Film, 'REMIND_MINUTES', 15)
    return clock


async def _beats(reminder, bot, hall, clock, minutes):
    """Heartbeats a minute apart for that many minutes, each with the gate's current state."""
    for _ in range(minutes):
        clock.now += 60
        await reminder.check(bot, await film.gate('GET'))


async def test_an_open_session_is_reminded_every_fifteen_minutes(hall, clock):
    bot, reminder = FakeBot(), film.Reminder()
    hall.state = {'active': True, 'code': '4817', 'started': 1, 'title': 'Сталкер'}
    await reminder.check(bot, hall.state)  # the session is seen: the start announced it
    await _beats(reminder, bot, hall, clock, 14)
    assert _said(bot) == []
    await _beats(reminder, bot, hall, clock, 1)
    assert _said(bot) == ['texts.film_remind_titled']
    await _beats(reminder, bot, hall, clock, 15)
    assert len(_said(bot)) == 2  # the cadence holds: 15 minutes, not 15 plus a heartbeat


async def test_an_untitled_session_gets_the_untitled_reminder(hall, clock):
    bot, reminder = FakeBot(), film.Reminder()
    hall.state = {'active': True, 'code': '4817', 'started': 1, 'title': None}
    await reminder.check(bot, hall.state)
    await _beats(reminder, bot, hall, clock, 15)
    assert _said(bot) == ['texts.film_remind']


async def test_a_new_session_starts_the_count_again(hall, clock):
    bot, reminder = FakeBot(), film.Reminder()
    hall.state = {'active': True, 'code': '4817', 'started': 1}
    await reminder.check(bot, hall.state)
    await _beats(reminder, bot, hall, clock, 10)
    hall.state = {'active': True, 'code': '5120', 'started': 2}  # stop and start in between
    await _beats(reminder, bot, hall, clock, 15)  # the first beat sees the new session, 14 more after it
    assert _said(bot) == []
    await _beats(reminder, bot, hall, clock, 1)
    assert _said(bot) == ['texts.film_remind']


async def test_no_reminder_without_a_session_or_when_switched_off(hall, clock, monkeypatch):
    bot, reminder = FakeBot(), film.Reminder()
    hall.state = {'active': False}
    await _beats(reminder, bot, hall, clock, 40)
    monkeypatch.setattr(Film, 'REMIND_MINUTES', 0)
    hall.state = {'active': True, 'code': '4817', 'started': 1}
    await _beats(reminder, bot, hall, clock, 40)
    assert _said(bot) == []


async def test_a_long_silent_gate_gives_one_reminder_not_a_burst(hall, clock):
    bot, reminder = FakeBot(), film.Reminder()
    hall.state = {'active': True, 'code': '4817', 'started': 1}
    await reminder.check(bot, hall.state)
    clock.now += 50 * 60  # the gate did not answer for 50 minutes
    await reminder.check(bot, hall.state)
    await _beats(reminder, bot, hall, clock, 14)
    assert len(_said(bot)) == 1
    await _beats(reminder, bot, hall, clock, 1)
    assert len(_said(bot)) == 2


async def test_a_failed_reminder_does_not_stop_the_heartbeat(hall, clock, monkeypatch, caplog):
    import asyncio
    bot = FakeBot()
    bot.send_chat_message = AsyncMock(side_effect=RuntimeError('chat down'))
    hall.state = {'active': True, 'code': '4817', 'started': 1}
    rounds = []

    async def sleep(_seconds):
        rounds.append(1)
        clock.now += 15 * 60
        if len(rounds) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(film.asyncio, 'sleep', sleep)
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await film.heartbeat_loop(bot)
    assert hall.calls == [('GET', None)] * 3  # the heartbeat kept going
    assert 'напоминание не ушло' in caplog.text


@pytest.mark.parametrize(('closed', 'said'), [(True, ['texts.film_shutdown_close']), (False, [])])
async def test_shutdown_closes_the_session_and_tells_chat_only_if_one_was_open(hall, monkeypatch, closed, said):
    calls = []

    async def gate(method, title=None, limit=None):
        calls.append((method, limit))
        return {'active': False, 'closed': closed}
    monkeypatch.setattr(film, 'gate', gate)
    bot = FakeBot()
    await film.close_on_shutdown(bot)
    assert calls == [('DELETE', film.SHUTDOWN_TIMEOUT)]
    assert _said(bot) == said


async def test_shutdown_with_the_gate_down_is_only_logged(hall):
    hall.error = film.GateError('down')
    bot = FakeBot()
    await film.close_on_shutdown(bot)
    bot.send_chat_message.assert_not_awaited()


def test_film_is_listed_by_help_channel_and_its_controls_are_not(hall):
    listing = chat_component(FakeBot())._channel.listing()
    assert '!film' in listing
    assert not {'!film start', '!film title', '!film stop'} & set(listing)


def test_without_the_hall_film_is_not_listed(monkeypatch):
    monkeypatch.setattr(Film, 'ENABLED', False)
    assert '!film' not in chat_component(FakeBot())._channel.listing()
