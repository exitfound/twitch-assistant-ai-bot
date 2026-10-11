"""Keeps the pulse of the Twitch chat fresh (src/gemini/pulse.py)."""
import asyncio
import logging

from src.core.activity import ChatWatch
from src.core.config import Pulse
from src.gemini import pulse
from src.twitch.core.port import BotPort

logger = logging.getLogger(__name__)


async def pulse_loop(bot: BotPort) -> None:
    """Every Pulse.INTERVAL_MINUTES during a stream rebuild the pulse, if anyone has written
    since the last one: a quiet chat keeps its pulse until it goes stale, at no cost. A build
    that gave nothing is tried again next time without waiting for a new message; offline
    chat costs no calls."""
    watch = ChatWatch()
    pending = False
    try:
        while True:
            # session_id is read once: the build outlives a switch of stream
            session_id = bot.session_id
            try:
                if bot.stream_live:
                    pending = await watch.new_messages(session_id) or pending
                    if pending:
                        pending = not await pulse.refresh(session_id)
            except Exception:
                logger.exception('Пульс чата не обновлён')
            await asyncio.sleep(Pulse.INTERVAL_MINUTES * 60)
    finally:
        logger.info('Пульс чата остановлен')
