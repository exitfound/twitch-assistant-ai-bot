"""The reminder: a pointer to !bot and !channel, only into a live conversation."""
import time

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core.activity import ChatWatch
from src.core.component import ChatComponent
from src.core.config import Follow
from src.core.database import save_chat_message
from src.local import help_announce


async def _talk(bot: FakeBot) -> None:
    await save_chat_message(bot.session_id, 'gop', 'привет', addressed=False)


async def test_the_pointer_goes_out_into_a_live_conversation(db):
    bot = FakeBot()
    await _talk(bot)
    await help_announce._announce(bot, ChatWatch(), time.monotonic())
    bot.send_chat_message.assert_awaited_once_with('texts.help_announce')


async def test_silent_chat_or_no_stream_gets_nothing(db):
    bot = FakeBot()
    await help_announce._announce(bot, ChatWatch(), time.monotonic())
    offline = FakeBot(stream_live=False)
    await _talk(offline)
    await help_announce._announce(offline, ChatWatch(), time.monotonic())
    bot.send_chat_message.assert_not_awaited()
    offline.send_chat_message.assert_not_awaited()


@pytest.mark.parametrize('command', ['!help', '!bot', '!channel'])
async def test_either_help_command_skips_the_next_pointer(db, command):
    bot = FakeBot()
    since = time.monotonic()
    await ChatComponent(bot).event_message(make_message(command, make_chatter('gop')))
    await _talk(bot)
    await help_announce._announce(bot, ChatWatch(), since)
    bot.send_chat_message.assert_not_awaited()


async def test_help_answers_with_the_reminder_text_to_anyone(db, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    bot = FakeBot()
    bot.follows = False
    message = make_message('!help', make_chatter('gop'))
    await ChatComponent(bot).event_message(message)
    message.respond.assert_awaited_once_with('texts.help_announce')
    assert bot.follow_checks == 0
