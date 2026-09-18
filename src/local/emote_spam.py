"""Спам эмотами: порция случайных эмотов в чат раз в интервал, без Gemini."""
import asyncio
import logging
import random

from src.core.config import Emote
from src.core.content import Content
from src.core.utils import random_delay

logger = logging.getLogger(__name__)


async def emote_spam_loop(bot) -> None:
    """Периодическая порция эмотов в чат."""
    try:
        while True:
            delay = random_delay(Emote.SPAM_INTERVAL_MIN_MINUTES, Emote.SPAM_INTERVAL_MAX_MINUTES)
            logger.debug('Следующая порция эмотов через %.1f мин', delay / 60)
            await asyncio.sleep(delay)
            try:
                emotes = Content.items('emotes')
                if not emotes:
                    continue
                count = random.randint(Emote.SPAM_MIN, Emote.SPAM_MAX)
                if len(emotes) >= count:
                    sample = random.sample(emotes, count)
                else:
                    sample = random.choices(emotes, k=count)
                await bot.send_chat_message(' '.join(sample))
            except Exception:
                logger.exception('Спам эмотами не отправлен')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Цикл спама эмотами остановлен')
