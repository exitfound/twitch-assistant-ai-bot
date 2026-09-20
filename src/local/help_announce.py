"""Command reminder: once per interval the bot tells chat what it can do on its own.

A viewer who joins mid-stream has nowhere to learn about !roll and the rest:
they would first have to think of asking for help. So the bot reminds on its own.

No Gemini: the text is taken from texts.help_announce as is.
"""
import asyncio
import logging
import time

from src.core.config import Help
from src.core.content import Content
from src.core.activity import ChatWatch

logger = logging.getLogger(__name__)

# When the bot last posted the command list in reply to !help-bot (time.monotonic()).
# The command itself is not written to the DB, so it marks itself here
_help_shown_at = 0.0


def note_help_shown() -> None:
    """The command list has just gone to chat via !help-bot."""
    global _help_shown_at
    _help_shown_at = time.monotonic()


async def help_loop(bot) -> None:
    """Periodic command reminder."""
    watch = ChatWatch()
    since = time.monotonic()
    try:
        while True:
            await asyncio.sleep(Help.ANNOUNCE_INTERVAL_MINUTES * 60)
            try:
                since = await _announce(bot, watch, since)
            except Exception:
                logger.exception('Напоминание о командах не отправлено')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Цикл напоминаний о командах остановлен')


async def _announce(bot, watch: ChatWatch, since: float) -> float:
    """Remind if there is anyone to remind. Returns the new `since` mark."""
    # Offline there is nobody to remind: chat is empty and the game commands are closed
    if not bot.stream_live:
        return since
    # Nobody has written since the previous reminder – that would be advertising
    # to itself, and two identical messages in a row at that
    if not await watch.new_messages(bot.session_id):
        return since
    now = time.monotonic()
    # The command list was already in chat in reply to !help-bot: no need to repeat
    if _help_shown_at > since:
        return now
    text = Content.text('help_announce')
    if text:
        await bot.send_chat_message(text)
    return now
