"""Bot lifecycle pieces that can be checked without Twitch."""

import asyncio

import aiohttp
import pytest
import twitchio

import bot as bot_module
from src.core import database


@pytest.mark.parametrize(('error', 'hint'), [
    (twitchio.HTTPException('x', status=401, extra={'message': 'invalid token'}), True),
    (twitchio.HTTPException('x', status=403, extra={'message': 'missing scope'}), True),
    (twitchio.HTTPException('x', status=409, extra={'message': 'already subscribed'}), False),
    (twitchio.HTTPException('x', status=503, extra={'message': 'down'}), False),
    (aiohttp.ClientConnectionError('reset'), False),
    (TimeoutError(), False),
    (KeyError('no token for this user'), True),
])
def test_oauth_hint_only_for_token_problems(error, hint):
    """A network error or a 5xx printed «no token, log in here» and sent the owner to
    re-authorise for nothing."""
    assert bot_module._token_problem(error) is hint


async def test_closed_database_is_not_reopened_behind_the_shutdown(db):
    """A task finishing after close_db() would open a new connection, and its non-daemon
    thread would keep the process from exiting."""
    await database.close_db()
    with pytest.raises(RuntimeError):
        await database.get_db()
    await database.init_db()
    assert await database.get_db() is not None


def _bare_bot(**tasks) -> bot_module.Bot:
    """A Bot without twitchio behind it: only the task slots stop_background_tasks() reads."""
    bot = object.__new__(bot_module.Bot)
    for name in ('_proactive_task', '_emote_spam_task', '_help_task', '_curse_lift_task',
                 '_stream_watch_task', '_memory_task', '_chat_watch_task', '_heartbeat_task'):
        setattr(bot, name, tasks.get(name))
    return bot


async def test_background_tasks_stop_once_and_quietly_after(caplog):
    """close() runs more than once on shutdown: the loops are cancelled and awaited the
    first time, and the later calls have nothing left to report."""
    loop = asyncio.create_task(asyncio.sleep(3600))
    bot = _bare_bot(_proactive_task=loop)
    caplog.set_level('INFO', logger=bot_module.logger.name)
    await bot.stop_background_tasks()
    await bot.stop_background_tasks()
    assert loop.cancelled()
    assert [r.getMessage() for r in caplog.records].count('Фоновые задачи остановлены: 1') == 1
