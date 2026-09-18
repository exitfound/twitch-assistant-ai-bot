"""Фоновое оповещение игры: снятие проклятия.

Периодической сводки «кто залупа стрима» здесь больше нет: каждый !roll и
итог награды и так называют залупу и китежанина.
"""
import asyncio
import logging

from src.core.config import Roll
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.local.roll import game

logger = logging.getLogger(__name__)

# Как часто проверять, не спало ли проклятие. Отсчёт до снятия идёт в минутах,
# так что точности в минуту хватает, а запрос по одной сессии копеечный
CURSE_LIFT_CHECK_SECONDS = 60


async def curse_lift_loop(bot) -> None:
    """Сообщает в чат, когда с игрока спало проклятие.

    Проклятие спадает, когда потолок пробыл на дне REWARD_CURSE_HOLD_MINUTES.
    Проклятие, не дошедшее до дна, живёт до конца сессии и молча уходит вместе
    с ней – сообщать тут нечего, новая сессия и так начинается без проклятий.
    """
    try:
        while True:
            await asyncio.sleep(CURSE_LIFT_CHECK_SECONDS)
            try:
                await _announce_curse_lifts(bot)
            except Exception:
                logger.exception('Оповещение о снятии проклятия не отправлено')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Цикл снятия проклятий остановлен')


async def _announce_curse_lifts(bot) -> None:
    session_id = bot.session_id
    for user in await game.lift_expired_curses(session_id):
        text = Content.text('roll_curse_lifted', user=user, max=Roll.MAX)
        # Проклятие уже снято и в выборку больше не попадёт: если сообщение
        # не ушло, повторять его не будем – игра от этого не ломается
        if text and await bot.send_chat_message(text):
            await save_bot_interaction(session_id, '_roll_', '[curse-lifted]', text)
