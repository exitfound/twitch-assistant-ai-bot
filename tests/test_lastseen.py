"""!lastseen: when a viewer last wrote in the channel's chat."""
import re
from datetime import datetime

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core import content
from src.core.config import Clock, Follow
from src.core.database import get_last_message_time, save_chat_message
from src.twitch.core.commands import Kind
from src.twitch.core.component import ChatComponent
from src.twitch.local.lastseen import when


def _texts(**templates: str) -> None:
    path = content.CONTENT_PATH
    text = path.read_text(encoding='utf-8')
    for key, value in templates.items():
        text = re.sub(rf'(### {key}\n).*', lambda m, v=value: m.group(1) + v, text)
    path.write_text(text, encoding='utf-8')


@pytest.fixture
def component(monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    return ChatComponent(FakeBot())


async def _say(component, text: str, chatter=None):
    message = make_message(text, chatter or make_chatter('asker'))
    await component.event_message(message)
    return message


async def test_the_last_real_message_counts_not_an_old_command_row(db):
    await save_chat_message('2026-11-18 21:00', 'gop', 'привет')
    first = await get_last_message_time('gop')
    # Old rows may still hold commands: they are not chat
    await db.execute("INSERT INTO chat_messages (session_id, username, message, created_at)"
                     " VALUES ('s', 'gop', '!roll', '2099-01-01 00:00:00')")
    assert await get_last_message_time('gop') == first
    assert await get_last_message_time('nobody') is None


async def test_a_known_nick_gets_the_time(db, component):
    _texts(lastseen='{target} {when}', lastseen_today='сегодня в {time}')
    await save_chat_message('2026-11-18 21:00', 'gop', 'привет')
    message = await _say(component, '!lastseen @Gop,')
    reply = message.respond.await_args.args[0]
    assert re.fullmatch(r'gop сегодня в \d\d:\d\d', reply)


async def test_an_unknown_nick_and_a_bad_argument(db, component):
    message = await _say(component, '!lastseen nobody')
    message.respond.assert_awaited_once_with('texts.lastseen_unknown')
    message = await _say(component, '!lastseen', make_chatter('other'))
    message.respond.assert_awaited_once_with('texts.lastseen_usage')
    # Nothing was answered, so the viewer is not held by a cooldown
    assert component.bot.cooldown_remaining('other', Kind.LOCAL) == 0
    message = await _say(component, '!lastseen два слова', make_chatter('third'))
    message.respond.assert_awaited_once_with('texts.lastseen_usage')


async def test_it_needs_a_follow_unlike_the_public_channel_commands(db, component):
    component.bot.follows = False
    message = await _say(component, '!lastseen gop')
    message.respond.assert_awaited_once_with('texts.follow_required')


def test_today_yesterday_and_an_older_date():
    _texts(lastseen_today='сегодня {time}', lastseen_yesterday='вчера {time}', lastseen_date='{date} {time}')
    now = datetime(2026, 11, 18, 22, 0, tzinfo=Clock.ZONE)
    assert when(datetime(2026, 11, 18, 0, 5, tzinfo=Clock.ZONE).timestamp(), now) == 'сегодня 00:05'
    assert when(datetime(2026, 11, 17, 23, 59, tzinfo=Clock.ZONE).timestamp(), now) == 'вчера 23:59'
    assert when(datetime(2026, 8, 3, 19, 30, tzinfo=Clock.ZONE).timestamp(), now) == '03.08.2026 19:30'


def test_lastseen_is_listed_by_help_channel(component):
    assert '!lastseen' in component._channel.listing()
