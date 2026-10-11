"""The channel's own commands: a fixed reply from the `channel` section of CONTENT.md."""
import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core import content
from src.twitch.core.component import ChatComponent
from src.core.config import Cooldown, Follow
from src.core.content import Content, validate_content
from src.twitch.local.channel import MESSAGE_MAX
from src.twitch.local.roll.command import handle_roll


def _channel(*lines: str) -> None:
    """Append a `channel` section to the stand-in CONTENT.md, as an edit while the bot runs."""
    path = content.CONTENT_PATH
    text = path.read_text(encoding='utf-8') + '\n## channel\nНаписано до первой команды – заметка.\n'
    path.write_text(text + '\n'.join(lines) + '\n', encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))


@pytest.fixture
def component(monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    return ChatComponent(FakeBot())


def test_commands_come_in_file_order_lowercased_and_without_empty_ones():
    _channel('### !tg', 'телега {user}', '### !Donate', 'донат', '### !empty', '', '### note', 'без «!»')
    assert Content.channel_commands() == {'!tg': 'телега {user}', '!donate': 'донат'}


def test_no_section_means_no_commands():
    assert Content.channel_commands() == {}


@pytest.mark.parametrize('text', ['!tg', '!TG', '!tg пж', 'сосурян !tg', '@sosuryan_bot !tg'])
def test_a_channel_command_is_found_bare_or_after_the_addressing(component, text):
    _channel('### !tg', 'телега')
    entry = component._route(make_message(text))[0]
    assert entry is not None and entry.trigger == '!tg'


@pytest.mark.parametrize('text', ['!tgx', 'tg', 'ну !tg'])
def test_near_misses_are_not_channel_commands(component, text):
    _channel('### !tg', 'телега')
    assert component._route(make_message(text))[0] is None


def test_a_command_added_while_the_bot_runs_works_at_once(component):
    assert component._route(make_message('!tg'))[0] is None
    _channel('### !tg', 'телега')
    assert component._route(make_message('!tg'))[0] is not None


def test_a_bot_command_of_the_same_name_wins_and_is_logged(monkeypatch, caplog):
    _channel('### !roll', 'чужой ролл')
    with caplog.at_level(logging.ERROR):
        component = ChatComponent(FakeBot())
    assert component._registry.resolve('!roll').handler is handle_roll
    assert '!roll' in caplog.text


async def test_the_reply_goes_to_a_viewer_who_does_not_follow(db, component):
    _channel('### !tg', 'телега для {user}')
    component.bot.follows = False
    message = make_message('!tg', make_chatter('gop'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('телега для gop')
    assert component.bot.follow_checks == 0


async def test_a_command_is_not_saved_as_chat(db, component):
    _channel('### !tg', 'телега')
    await component.event_message(make_message('!tg', make_chatter('gop')))
    rows = await (await db.execute('SELECT COUNT(*) FROM chat_messages')).fetchone()
    assert rows[0] == 0


async def test_one_answer_per_command_for_the_whole_chat(db, component):
    """The reply is the same for everyone and still in chat: a repeat is dropped silently."""
    _channel('### !tg', 'телега', '### !donate', 'донат')
    first, second = make_message('!tg', make_chatter('gop')), make_message('!tg', make_chatter('kek'))
    other = make_message('!donate', make_chatter('kek'))
    for message in (first, second, other):
        await component.event_message(message)
    first.respond.assert_awaited_once_with('телега')
    second.respond.assert_not_awaited()
    other.respond.assert_awaited_once_with('донат')


async def test_a_channel_command_takes_no_personal_cooldown(db, component, monkeypatch):
    """!tg must not hold back !roll, nor the other way round."""
    _channel('### !tg', 'телега')
    stat = AsyncMock()
    monkeypatch.setattr(component._registry.resolve('!stat'), 'handler', stat)
    await component.event_message(make_message('!stat', make_chatter('gop')))
    message = make_message('!tg', make_chatter('gop'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('телега')
    await component.event_message(make_message('!stat', make_chatter('kek')))
    assert stat.await_count == 2


async def test_the_broadcaster_and_a_zero_setting_skip_the_shared_pause(db, component, monkeypatch):
    _channel('### !tg', 'телега')
    await component.event_message(make_message('!tg', make_chatter('gop')))
    streamer = make_message('!tg', make_chatter('exitfound', broadcaster=True))
    await component.event_message(streamer)
    streamer.respond.assert_awaited_once_with('телега')
    monkeypatch.setattr(Cooldown, 'PUBLIC', 0)
    again = make_message('!tg', make_chatter('kek'))
    await component.event_message(again)
    again.respond.assert_awaited_once_with('телега')


@pytest.mark.parametrize('command', ['!help', '!bot', '!channel'])
async def test_help_commands_share_the_same_pause(db, component, command):
    first, second = make_message(command, make_chatter('gop')), make_message(command, make_chatter('kek'))
    await component.event_message(first)
    await component.event_message(second)
    first.respond.assert_awaited_once()
    second.respond.assert_not_awaited()


async def test_a_command_removed_before_the_answer_says_nothing(db, component):
    _channel('### !tg', 'телега')
    entry = component._registry.resolve('!tg')
    path = content.CONTENT_PATH
    path.write_text(path.read_text(encoding='utf-8').replace('### !tg\nтелега\n', ''), encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    ctx = MagicMock(user='gop', message=make_message('!tg'))
    await entry.handler(ctx)
    ctx.message.respond.assert_not_awaited()


def test_the_section_is_known_and_a_key_without_the_mark_warns(caplog):
    _channel('### !tg', 'телега', '### tg', 'без «!»')
    with caplog.at_level(logging.WARNING):
        validate_content()
    assert 'channel.tg' in caplog.text
    assert '## channel' not in caplog.text


async def test_help_channel_lists_the_commands_in_file_order(db, component):
    _channel('### !tg', 'телега', '### !donate', 'донат')
    component.bot.follows = False
    message = make_message('!channel', make_chatter('gop'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.help_channel')


async def test_help_channel_fills_the_list(db, component):
    path = content.CONTENT_PATH
    path.write_text(path.read_text(encoding='utf-8').replace(
        '### help_channel\ntexts.help_channel', '### help_channel\n@{user} – {commands}'), encoding='utf-8')
    _channel('### !tg', 'телега', '### !donate', 'донат')
    message = make_message('!channel', make_chatter('gop'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('@gop – !tg | !donate')


async def test_help_channel_without_commands(db, component):
    message = make_message('!channel', make_chatter('gop'))
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.help_channel_empty')


def test_a_clash_added_while_the_bot_runs_is_logged_at_the_next_message(component, caplog):
    _channel('### !roll', 'чужой ролл')
    with caplog.at_level(logging.ERROR):
        component._route(make_message('привет'))
        component._route(make_message('ещё раз'))
    assert caplog.text.count('!roll') == 1


def test_a_reply_or_a_list_too_long_for_twitch_is_logged(component, caplog):
    path = content.CONTENT_PATH
    path.write_text(path.read_text(encoding='utf-8').replace(
        '### help_channel\ntexts.help_channel', '### help_channel\n@{user} – {commands}'), encoding='utf-8')
    many = [line for i in range(60) for line in (f'### !cmd{i}', 'ок')]
    _channel('### !long', 'x' * (MESSAGE_MAX + 1), *many)
    with caplog.at_level(logging.WARNING):
        component._route(make_message('привет'))
    assert '!long' in caplog.text
    assert 'список !channel' in caplog.text
