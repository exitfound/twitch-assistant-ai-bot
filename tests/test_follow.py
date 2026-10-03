"""Thanks for a follow: once per viewer, a few a minute, never a stop-listed login."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBot
from src.core import content
from src.core.database import save_bot_interaction
from src.local import follow


def _follow(name: str) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(name=name, id=f'id-{name}'))


async def test_a_follower_is_thanked_through_the_bot(db):
    """Through send_chat_message, which defuses the line like every other one the bot writes."""
    bot = FakeBot()
    await follow.handle_follow(bot, _follow('gop'))
    bot.send_chat_message.assert_awaited_once_with('follow gop')


async def test_a_repeated_follow_is_thanked_once(db):
    """EventSub may deliver the event twice, and unfollow-follow loops are a raid trick."""
    bot = FakeBot()
    await follow.handle_follow(bot, _follow('gop'))
    await follow.handle_follow(bot, _follow('gop'))
    bot.send_chat_message.assert_awaited_once()


async def test_two_deliveries_at_once_are_thanked_once(db):
    """Each event runs in its own task: both would pass the database check before
    either saved its greeting."""
    bot = FakeBot()
    await asyncio.gather(follow.handle_follow(bot, _follow('gop')), follow.handle_follow(bot, _follow('gop')))
    bot.send_chat_message.assert_awaited_once()


async def test_a_viewer_thanked_before_a_restart_is_not_thanked_again(db):
    await save_bot_interaction('2026-09-01 20:00', 'gop', '[follow]', 'follow gop')
    bot = FakeBot()
    await follow.handle_follow(bot, _follow('gop'))
    bot.send_chat_message.assert_not_awaited()


async def test_a_follow_raid_gets_a_few_thanks_a_minute(db):
    bot = FakeBot()
    for i in range(20):
        await follow.handle_follow(bot, _follow(f'bot{i}'))
    assert bot.send_chat_message.await_count == follow.GREETINGS_PER_MINUTE


async def test_a_stop_listed_login_is_not_said_in_chat(db):
    path = Path(content.CONTENT_PATH)
    path.write_text(path.read_text(encoding='utf-8').replace('### banned\n', '### banned\nnazi\n'),
                    encoding='utf-8')
    bot = FakeBot()
    await follow.handle_follow(bot, _follow('Real_Nazi_1488'))
    bot.send_chat_message.assert_not_awaited()
