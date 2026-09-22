import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable
from datetime import datetime
from typing import TYPE_CHECKING

from src.core.config import Clock

if TYPE_CHECKING:
    import twitchio

logger = logging.getLogger(__name__)


def local_time(ts: float | None = None) -> datetime:
    """A moment in the bot's zone (BOT_TIMEZONE), now by default – not the process's zone."""
    return datetime.fromtimestamp(time.time() if ts is None else ts, Clock.ZONE)


# Addressing the bot by word: «сосур» in Cyrillic and «secur» in Latin, one entry per
# variant. It lives here rather than in the dispatcher because src/core/database.py
# uses the same pattern to mark addressings in stored chat messages.
SOSUR_VARIANTS = ('сосур', 'secur')
SOSUR_RE = re.compile(
    r'(?:{})\w*'.format('|'.join(SOSUR_VARIANTS)), re.IGNORECASE | re.UNICODE
)

def random_delay(min_minutes: float, max_minutes: float) -> float:
    """A random pause in seconds between min and max minutes.

    A fixed interval in chat reads as a schedule: viewers notice the bot posts
    every N minutes. The spread makes it feel more alive.
    """
    return random.uniform(min(min_minutes, max_minutes), max(min_minutes, max_minutes)) * 60


# What to strip around a nick in a command argument: «!who @ник,» is the nick «ник»
NICK_TRAILING = ',.:;!?'
# A Twitch login is at most 25 characters: anything longer is not a nick
NICK_MAX = 25


def clean_nick(raw: str) -> str:
    """A nick from a command argument: no @, no trailing punctuation, lowercased.

    Cut to NICK_MAX because the answer echoes it back («@ник, @цель ни разу не писал»):
    a Twitch login is at most 25 characters, so nothing real is lost, and an argument of
    arbitrary length cannot be turned into a message of the viewer's choosing.
    """
    return raw.lstrip('@').rstrip(NICK_TRAILING).lower()[:NICK_MAX]


def defuse(text: str) -> str:
    """Make a line safe to send on its own, with no nick in front of it.

    A message starting with `/` or `.` reads as a chat command. The Helix endpoint the
    bot sends through does not execute them, so this is about how it looks in chat,
    not about privileges.
    """
    return text.lstrip('/.').lstrip()


def safe_format(template: str, **values) -> str:
    """Fill a template from the file. A broken template does not crash the handler."""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError, AttributeError):
        logger.warning('Не удалось подставить значения в шаблон: %r', template[:80])
        return template


def reply_to_bot(message: 'twitchio.ChatMessage', bot_id: str | int | None) -> str | None:
    """The bot's line this chat message replies to, or None if it is not a reply to the bot.

    twitchio's ChatMessageReply carries parent_user (a PartialUser), not
    parent_user_id: reading the latter yields '' and no reply is ever recognised,
    which only stays invisible because Twitch puts @botname at the start of a reply.
    """
    reply = getattr(message, 'reply', None)
    parent = getattr(reply, 'parent_user', None)
    if parent is None or str(getattr(parent, 'id', '')) != str(bot_id):
        return None
    return getattr(reply, 'parent_message_body', None) or None


async def gather_cancelling(*aws: Awaitable) -> list:
    """Like asyncio.gather(), but the first error cancels the rest and is raised as is.

    gather() leaves the siblings running after an error: they keep spending Gemini calls,
    or reopen the database after close_db(). A bare TaskGroup would raise an
    ExceptionGroup, which the callers' `except Unavailable` does not catch.
    """
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(aw) for aw in aws]
    except ExceptionGroup as errors:
        raise errors.exceptions[0] from None
    return [task.result() for task in tasks]
