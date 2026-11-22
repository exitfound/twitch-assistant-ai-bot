"""The voice service's process: the Discord client, its voice and the API the bot speaks through."""
import asyncio
import contextlib
import logging
import signal

from src.core.config import Discord, Service
from src.core.content import validate_content
from src.core.database import close_db, init_db
from src.core.logging_setup import setup_logging
from src.core.service import serve
from src.discord import service
from src.discord.bot import DiscordService

logger = logging.getLogger(__name__)


def _missing() -> list[str]:
    return ['DISCORD_TOKEN'] if not Discord.TOKEN else Discord.missing()


async def run() -> None:
    setup_logging('INFO')
    validate_content()
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(name: str) -> None:
        logger.info('Получен %s, завершаюсь...', name)
        shutdown.set()

    loop.add_signal_handler(signal.SIGTERM, _on_signal, 'SIGTERM')
    loop.add_signal_handler(signal.SIGINT, _on_signal, 'SIGINT')
    missing = _missing()
    if missing:
        # Waits instead of exiting: compose would restart it in a loop, and the voice is optional
        logger.error('Голос не запущен: не заданы %s – служба ждёт остановки', ', '.join(missing))
        await shutdown.wait()
        return
    discord_service = DiscordService()
    tasks: list[asyncio.Task] = []
    try:
        # Walked by every service against the one database: the steps are idempotent
        await init_db()
        tasks.append(asyncio.create_task(discord_service.run(), name='discord'))
        tasks.append(asyncio.create_task(
            serve(service.app(), Service.VOICE_API_HOST, Service.VOICE_API_PORT, 'API голоса'), name='api'))
        await shutdown.wait()
    finally:
        try:
            await discord_service.stop()
        except Exception:
            logger.exception('Discord закрылся с ошибкой')
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await close_db()
