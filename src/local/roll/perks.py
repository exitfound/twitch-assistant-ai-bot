"""Game perks from the previous stream: a shield to the китежанин (champion), a curse to
the залупа (loser).

The mechanics are in src/local/roll/game.py. Only chat lives here: announce the perks at
the stream start and tell a player their countdown has started when they first write in chat.
"""
import logging

from src.core.config import Rewards, Roll
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.core.port import BotPort
from src.local.roll import game
from src.local.roll.storage import get_pending_perk_users

logger = logging.getLogger(__name__)

# Who got a perk this session but has not shown up yet. Without the cache every chat
# message would hit the DB. A stale cache is harmless: if a reroll has already started
# the countdown, the extra call returns nothing
_pending: dict[str, set[str]] = {}


async def on_stream_start(bot: BotPort, session_id: str) -> None:
    """The stream is live: grant the perks from the previous one and announce them in chat.

    Safe to call again – after a restart or an outage, what was already granted is
    neither granted nor announced a second time.
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


async def on_chat(bot: BotPort, session_id: str, user: str) -> None:
    """A chat message: if a perk awaits the player, start the countdown and say so."""
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
            key = 'roll_perk_shield_on' if perk == game.Perk.SHIELD else 'roll_perk_curse_on'
            await _say(bot, session_id, f'[perk:{perk}]', Content.text(
                key, user=user, minutes=Roll.PERK_MINUTES,
                ceiling=Rewards.CURSE_CEILING, step=Rewards.CURSE_STEP,
            ))
    except Exception:
        logger.exception('Бонус игрока %s не запущен', user)


async def _say(bot: BotPort, session_id: str, tag: str, text: str) -> None:
    if text and await bot.send_chat_message(text):
        await save_bot_interaction(session_id, '_roll_', tag, text)
