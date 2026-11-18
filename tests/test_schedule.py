"""!schedule: the nearest streams from the channel's schedule on Twitch."""
import re
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock

import pytest
import twitchio

from fakes import FakeBot, make_chatter, make_message
from src.core import content
from src.core.config import Follow
from src.twitch.core.component import ChatComponent
from src.twitch.local import schedule
from src.twitch.local.schedule import Plan, Slot, parse, render

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _at(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')


def _segment(hours: float, *, title: str = '', category: str | None = None, canceled: bool = False) -> dict:
    return {'start_time': _at(hours), 'end_time': _at(hours + 4), 'title': title,
            'canceled_until': _at(hours + 4) if canceled else None,
            'category': {'id': '1', 'name': category} if category else None, 'is_recurring': True}


def _texts(**templates: str) -> None:
    """Real templates for some texts and lists of the stand-in CONTENT.md."""
    path = content.CONTENT_PATH
    text = path.read_text(encoding='utf-8')
    for key, value in templates.items():
        text = re.sub(rf'(### {key}\n).*', lambda m, v=value: m.group(1) + v, text)
    path.write_text(text, encoding='utf-8')


@pytest.fixture
def component(monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    return ChatComponent(FakeBot())


def test_past_and_cancelled_slots_are_skipped_and_the_nearest_taken():
    plan = parse({'segments': [_segment(50), _segment(-2), _segment(1, canceled=True), _segment(3)]}, NOW)
    assert [slot.start for slot in plan.slots] == [NOW + timedelta(hours=3)]
    assert plan.vacation_until is None


def test_slots_are_capped_and_sorted(monkeypatch):
    monkeypatch.setattr(schedule, 'SLOTS', 3)
    plan = parse({'segments': [_segment(hours) for hours in (5, 1, 4, 2, 3)]}, NOW)
    assert [slot.start.hour for slot in plan.slots] == [13, 14, 15]


@pytest.mark.parametrize('data', [{}, {'segments': None, 'vacation': None}])
def test_an_empty_schedule_has_no_slots(data):
    assert parse(data, NOW) == Plan([], None)


def test_a_vacation_counts_only_while_it_lasts():
    on = {'start_time': _at(-24), 'end_time': _at(72)}
    ahead = {'start_time': _at(24), 'end_time': _at(72)}
    assert parse({'segments': [], 'vacation': on}, NOW).vacation_until == NOW + timedelta(hours=72)
    assert parse({'segments': [], 'vacation': ahead}, NOW).vacation_until is None


def test_slots_are_shown_in_the_bots_zone_with_the_weekday():
    _texts(schedule='{slots} | {url}', schedule_slot='{weekday} {date} в {time} {title}',
           weekdays='пн\nвт\nср\nчт\nпт\nсб\nвс')
    plan = Plan([Slot(datetime(2026, 11, 20, 17, 0, tzinfo=UTC), '', '')], None)
    # BOT_TIMEZONE defaults to Europe/Moscow, UTC+3; the empty title leaves no space behind
    assert render(plan, 'gop') == 'пт 20.11 в 20:00 | https://www.twitch.tv/testchannel/schedule'


def test_a_weekday_list_of_the_wrong_length_drops_the_weekday():
    _texts(schedule='{slots}', schedule_slot='{weekday} {date}', weekdays='пн\nвт')
    plan = Plan([Slot(datetime(2026, 11, 20, 17, 0, tzinfo=UTC), '', '')], None)
    assert render(plan, 'gop') == '20.11'


def test_vacation_empty_and_failure_have_their_own_texts():
    _texts(schedule_vacation='до {until}', schedule_empty='пусто {user}', schedule_failed='{url}')
    until = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    assert render(Plan([Slot(until, '', '')], until), 'gop') == 'до 25.11'
    assert render(Plan([], None), 'gop') == 'пусто gop'
    assert render(None, 'gop') == 'https://www.twitch.tv/testchannel/schedule'


async def test_fetch_reads_helix_and_takes_404_for_no_schedule():
    http = AsyncMock()
    http.request_json.return_value = {'data': {'segments': [_segment(3)], 'vacation': None}}
    assert (await schedule.fetch(http, '2000'))['segments'][0]['start_time'] == _at(3)
    http.request_json.side_effect = twitchio.HTTPException('нет', status=404, extra='')
    assert await schedule.fetch(http, '2000') == {}
    http.request_json.side_effect = twitchio.HTTPException('сбой', status=500, extra='')
    assert await schedule.fetch(http, '2000') is None
    http.request_json.side_effect = TimeoutError()
    assert await schedule.fetch(http, '2000') is None


async def test_a_viewer_without_a_follow_gets_the_schedule(db, component):
    component.bot.follows = False
    message = make_message('!schedule', make_chatter('gop'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.schedule_empty')
    assert component.bot.follow_checks == 0


async def test_the_schedule_is_cached_but_a_failure_is_not(db, component):
    bot = component.bot
    bot.fetch_schedule.return_value = None
    first = make_message('!schedule', make_chatter('gop'))
    await schedule.handle_schedule(_ctx(bot, first))
    first.respond.assert_awaited_once_with('texts.schedule_failed')
    bot.fetch_schedule.return_value = {'segments': [_segment(1000)]}
    await schedule.handle_schedule(_ctx(bot, make_message('!schedule')))
    await schedule.handle_schedule(_ctx(bot, make_message('!schedule')))
    assert bot.fetch_schedule.await_count == 2


def test_schedule_is_listed_by_help_channel(component):
    assert '!schedule' in component._channel.listing()


def _ctx(bot, message):
    from src.twitch.core.commands import CommandContext
    return CommandContext(message=message, user=message.chatter.name, prompt='!schedule',
                          original_text='!schedule', session_id=bot.session_id, bot=bot)


def test_slots_inside_an_upcoming_vacation_are_skipped():
    vacation = {'start_time': _at(24), 'end_time': _at(96)}
    plan = parse({'segments': [_segment(30), _segment(50), _segment(100)], 'vacation': vacation}, NOW)
    assert [slot.start for slot in plan.slots] == [NOW + timedelta(hours=100)]
    assert plan.vacation_until is None


def test_a_time_without_an_offset_is_taken_as_utc():
    naive = (NOW + timedelta(hours=3)).replace(tzinfo=None).isoformat()
    plan = parse({'segments': [{'start_time': naive, 'canceled_until': None}]}, NOW)
    assert [slot.start for slot in plan.slots] == [NOW + timedelta(hours=3)]
