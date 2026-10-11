"""Entry point: the CLI commands, or the bot itself on every platform it is configured for."""
import asyncio
import contextlib
import logging
import signal

from src.core.config import Discord, validate_config
from src.core.content import validate_content
from src.core.database import close_db, init_db
from src.core.logging_setup import setup_logging
from src.discord.bot import DiscordService
from src.twitch.bot import Bot

logger = logging.getLogger(__name__)


def start_discord() -> tuple[DiscordService, asyncio.Task] | None:
    """Discord beside Twitch, whenever its token is set; it never stops Twitch."""
    if not Discord.TOKEN:
        return None
    missing = Discord.missing()
    if missing:
        logger.error('Discord не запущен: не заданы %s', ', '.join(missing))
        return None
    service = DiscordService()
    return service, asyncio.create_task(service.run(), name='discord')


async def stop_discord(started: tuple[DiscordService, asyncio.Task] | None) -> None:
    if started is None:
        return
    service, task = started
    try:
        await service.stop()
    except Exception:
        logger.exception('Discord закрылся с ошибкой')
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def run_bot() -> None:
    setup_logging('INFO')
    validate_config()
    validate_content()
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(name: str) -> None:
        # Logged here: otherwise a stop on a signal is indistinguishable from a
        # process that stopped for its own reasons
        logger.info('Получен %s, завершаюсь...', name)
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _on_signal, 'SIGTERM')
    loop.add_signal_handler(signal.SIGINT, _on_signal, 'SIGINT')
    discord_bot = None
    try:
        # The schema is brought up to date before any platform starts: both write to the
        # database, and a write during a migration would roll back its unfinished work
        await init_db()
        discord_bot = start_discord()
        async with Bot() as bot:
            bot.on_shutdown_signal = _on_signal
            bot_task = asyncio.create_task(bot.start())
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            done, _ = await asyncio.wait(
                [bot_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot_task
            else:
                # The bot stopped on its own. Its exception has to be taken out of the
                # task, otherwise the process exits as if nothing happened and the
                # traceback surfaces only as «Task exception was never retrieved»
                shutdown_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception('Бот остановился из-за ошибки')
            await bot.stop_background_tasks()
    finally:
        await stop_discord(discord_bot)
        await close_db()


if __name__ == '__main__':
    from src.cli.main import main as cli_main
    if not cli_main():
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run_bot())
