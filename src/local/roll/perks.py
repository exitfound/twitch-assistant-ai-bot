"""Бонусы игры по итогам прошлого эфира: щит китежанину, проклятие залупе.

Механика – в src/local/roll/game.py. Здесь только чат: объявить бонусы в начале
эфира и сказать игроку, что его отсчёт пошёл, когда он впервые пишет в чат.
"""
import logging

from src.core.config import Rewards, Roll
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.local.roll import game
from src.local.roll.storage import get_pending_perk_users

logger = logging.getLogger(__name__)

# Кто в сессии получил бонус, но ещё не появлялся. Без кэша каждое сообщение
# чата ходило бы в базу. Устаревший кэш безвреден: если отсчёт уже запустил
# переброс, лишний вызов вернёт пусто
_pending: dict[str, set[str]] = {}


async def on_stream_start(bot, session_id: str) -> None:
    """Эфир идёт: выдать бонусы по итогам прошлого и объявить в чат.

    Безопасно звать повторно – после перезапуска или обрыва уже выданное не
    выдаётся и не объявляется второй раз.
    """
    if not Roll.PERKS_ENABLED:
        return
    try:
        granted = await game.grant_perks(session_id)
        _pending.pop(session_id, None)
        if granted is None:
            return
        champion, loser = granted
        if champion and loser:
            key = 'roll_perks_both'
        elif champion:
            key = 'roll_perks_champion'
        else:
            key = 'roll_perks_loser'
        await _say(bot, session_id, '[perks]', Content.text(
            key, champion=champion, loser=loser, minutes=Roll.PERK_MINUTES,
        ))
    except Exception:
        logger.exception('Бонусы по итогам прошлого эфира не выданы')


async def on_chat(bot, session_id: str, user: str) -> None:
    """Сообщение в чат: если игрока ждёт бонус, запустить отсчёт и сказать об этом."""
    if not Roll.PERKS_ENABLED or not bot.stream_live:
        return
    try:
        pending = _pending.get(session_id)
        if pending is None:
            pending = _pending[session_id] = await get_pending_perk_users(session_id)
        if user not in pending:
            return
        pending.discard(user)
        for perk in await game.appear(session_id, user):
            key = 'roll_perk_shield_on' if perk == game.PERK_SHIELD else 'roll_perk_curse_on'
            await _say(bot, session_id, f'[perk:{perk}]', Content.text(
                key, user=user, minutes=Roll.PERK_MINUTES,
                ceiling=Rewards.CURSE_CEILING, step=Rewards.CURSE_STEP,
            ))
    except Exception:
        logger.exception('Бонус игрока %s не запущен', user)


async def _say(bot, session_id: str, tag: str, text: str) -> None:
    if text and await bot.send_chat_message(text):
        await save_bot_interaction(session_id, '_roll_', tag, text)
