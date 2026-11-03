"""The choking flask's steps: a background loop takes them off the rolls as they fall due.

Chat hears nothing of a step; the OBS page gets the titles only when a step changed them.
"""
import asyncio
import logging

from src.twitch.core.port import BotPort
from src.twitch.local.roll import game, overlay

logger = logging.getLogger(__name__)

# How often to look for due steps: a step falls every minute, so it lands within a few
# seconds of its time, and a query over one session is cheap
CHOKE_CHECK_SECONDS = 5


async def choke_loop(bot: BotPort) -> None:
    """Takes the choking flask's due steps off the rolls while a stream is live."""
    try:
        while True:
            await asyncio.sleep(CHOKE_CHECK_SECONDS)
            if not bot.stream_live:
                continue
            try:
                standings = await game.tick_chokes(bot.session_id)
                if standings is not None:
                    overlay.publish(overlay.choked(standings))
            except Exception:
                logger.exception('Шаг ФЛАСКИ УДУШЕНИЯ не снят')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Цикл ФЛАСКИ УДУШЕНИЯ остановлен')
