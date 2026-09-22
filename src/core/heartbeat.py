"""The liveness file the container healthcheck reads."""
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60


async def heartbeat_loop(path: Path) -> None:
    """Touch a file so the container healthcheck can see the bot is still running.

    Started only when BOT_HEARTBEAT is set, so nothing changes outside a container, and
    only together with the other loops: a process that came up without the channel id
    and the loops is alive but deaf, and must read as unhealthy.
    """
    logger.info('Heartbeat включён: %s (раз в %d с)', path, HEARTBEAT_SECONDS)
    while True:
        try:
            path.touch()
        except OSError as e:
            # A full or read-only volume: say so, but do not take the bot down
            logger.warning('Не удалось обновить heartbeat: %s', e)
        await asyncio.sleep(HEARTBEAT_SECONDS)
