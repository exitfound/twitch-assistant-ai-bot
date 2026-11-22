"""The viewer's status in the stream, as chat last showed it.

A channel-points redemption carries no badges, so the rewards read the status from here:
the follow gate, the buyer's limits and the free throws of the extra roll. Written on the
first message in the stream and again whenever the badges change.
"""
import logging

from src.roll.port import RollPort
from src.core.tier import Tier
from src.roll.storage import save_tier

logger = logging.getLogger(__name__)

# The status written for each viewer in the current session: without it every chat
# message would be a database write
_written: dict[str, dict[str, str]] = {}


async def on_chat(bot: RollPort, session_id: str, user: str, tier: Tier) -> None:
    """A chat message: store the viewer's status if it is new for this stream."""
    if not bot.stream_live:
        return
    if session_id not in _written:
        _written.clear()
        _written[session_id] = {}
    written = _written[session_id]
    if written.get(user) == tier:
        return
    try:
        await save_tier(session_id, user, tier)
        written[user] = tier
    except Exception:
        logger.exception('Статус %s не записан', user)
