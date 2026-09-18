"""Напоминание о командах: бот сам пишет в чат, что умеет, раз в интервал.

Зритель, зашедший посреди эфира, про !roll и остальное не узнает ниоткуда:
справку он должен сначала догадаться спросить. Поэтому бот напоминает сам.

Без Gemini: текст берётся из texts.help_announce, как есть.
"""
import asyncio
import logging

from src.core.config import Help
from src.core.content import Content
from src.core.database import get_recent_chat

logger = logging.getLogger(__name__)

# Сколько последних сообщений сессии смотрим, чтобы понять, есть ли кто живой
ACTIVITY_WINDOW = 1


async def help_loop(bot) -> None:
    """Периодическое напоминание о командах."""
    try:
        while True:
            await asyncio.sleep(Help.ANNOUNCE_INTERVAL_MINUTES * 60)
            try:
                await _announce(bot)
            except Exception:
                logger.exception('Напоминание о командах не отправлено')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Цикл напоминаний о командах остановлен')


async def _announce(bot) -> None:
    # Вне эфира напоминать некому: чат пустой, а команды игры всё равно закрыты
    if not bot.stream_live:
        return
    # Пустой чат – тот же случай: это была бы реклама самому себе
    if not await get_recent_chat(bot.session_id, ACTIVITY_WINDOW):
        return
    text = Content.text('help_announce')
    if text:
        await bot.send_chat_message(text)
