"""The game service: commands, chat and stream events, redemptions."""
from collections.abc import Awaitable, Callable

from src.roll import announce, board, overlay, perks
from src.roll.command import handle_roll, handle_rollstat
from src.roll.port import RollPort
from src.roll.request import Request
from src.roll.storage import get_applied

HANDLERS: dict[str, Callable[[Request], Awaitable[None]]] = {
    '!roll': handle_roll,
    '!rollstat': handle_rollstat,
    board.TRIGGER: board.handle_rollboard,
}


async def on_stream(bot: RollPort, session_id: str, *, live: bool) -> None:
    """The stream started, ended, or the bot started: titles, regalia, old curses, perks.

    Safe to repeat: every step is idempotent, and the champion's Благодать, held back
    while the rewards were down, is granted once they come up and the bot calls this again.
    """
    await overlay.refresh(session_id, live=live)
    await board.regalia(session_id, live=live)
    if live:
        # The curses of finished streams go first: the perks granted below lay new ones
        await announce.on_stream_start(session_id)
        await perks.on_stream_start(bot, session_id)


async def applied(redemption_ids: list[str]) -> set[str]:
    return await get_applied(redemption_ids)
