"""The channel's own commands: a fixed reply from the `channel` section of CONTENT.md."""
import logging
import os
from unittest.mock import MagicMock

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core import content
from src.core.component import ChatComponent
from src.core.config import Follow
from src.core.content import Content, validate_content
from src.local.roll.command import handle_roll


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


async def test_the_local_cooldown_holds_a_repeat(db, component):
    _channel('### !tg', 'телега')
    first, second = make_message('!tg', make_chatter('gop')), make_message('!tg', make_chatter('gop'))
    await component.event_message(first)
    await component.event_message(second)
    first.respond.assert_awaited_once_with('телега')
    second.respond.assert_awaited_once_with('texts.cooldown_local')


async def test_a_command_removed_before_the_answer_gives_the_cooldown_back(db, component):
    _channel('### !tg', 'телега')
    entry = component._registry.resolve('!tg')
    path = content.CONTENT_PATH
    path.write_text(path.read_text(encoding='utf-8').replace('### !tg\nтелега\n', ''), encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))
    ctx = MagicMock(user='gop', message=make_message('!tg'))
    await entry.handler(ctx)
    ctx.clear_cooldown.assert_called_once()
    ctx.message.respond.assert_not_awaited()


def test_the_section_is_known_and_a_key_without_the_mark_warns(caplog):
    _channel('### !tg', 'телега', '### tg', 'без «!»')
    with caplog.at_level(logging.WARNING):
        validate_content()
    assert 'channel.tg' in caplog.text
    assert '## channel' not in caplog.text
