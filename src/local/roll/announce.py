"""Background announcement of the game: a curse lift.

There is no periodic «кто залупа стрима» summary: every !roll and reward outcome
already names the loser (залупа) and the champion (китежанин).
"""
import asyncio
import logging

from src.core.config import Roll
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.local.roll import game

logger = logging.getLogger(__name__)

# How often to check whether a curse has lifted. The countdown runs in minutes,
# so one-minute precision is enough, and a query over one session is cheap
CURSE_LIFT_CHECK_SECONDS = 60


async def curse_lift_loop(bot) -> None:
    """Tells chat when a player's curse has lifted.

    A curse lifts once the ceiling has sat on the floor for REWARD_CURSE_HOLD_MINUTES.
    A curse that never reaches the floor lives until the session ends and leaves
    silently with it – nothing to announce, a new session starts without curses anyway.
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
        # The curse is already cleared and will not be selected again, so a message
        # that did not go out is not repeated – the game does not break from that
        if text and await bot.send_chat_message(text):
            await save_bot_interaction(session_id, '_roll_', '[curse-lifted]', text)
