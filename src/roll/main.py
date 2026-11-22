"""The game service's process: its API for the bot, the OBS overlay, the background loops."""
import asyncio
import logging
import signal

from aiohttp import web

from src.core import sse
from src.core.config import Rewards, Roll, Service, Twitch
from src.core.content import validate_content
from src.core.database import close_db, init_db
from src.core.logging_setup import setup_logging
from src.core.service import ServiceClient, serve
from src.core.tasks import BackgroundTasks
from src.roll import overlay
from src.roll.announce import curse_lift_loop
from src.roll.choke import choke_loop
from src.roll.remote import RemoteBot
from src.roll.service import GameService, on_stream

logger = logging.getLogger(__name__)


def _check_config() -> None:
    # The game knows the broadcaster by login and reaches Twitch only through the bot
    missing = [name for name, value in (('TWITCH_CHANNEL', Twitch.CHANNEL), ('BOT_API_URL', Service.BOT_API_URL))
               if not value]
    if missing:
        raise OSError(f'Missing required env vars: {", ".join(missing)}')


async def run() -> None:
    setup_logging('INFO')
    _check_config()
    validate_content()
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(name: str) -> None:
        logger.info('Получен %s, завершаюсь...', name)
        shutdown.set()

    loop.add_signal_handler(signal.SIGTERM, _on_signal, 'SIGTERM')
    loop.add_signal_handler(signal.SIGINT, _on_signal, 'SIGINT')
    client = ServiceClient('бот', Service.BOT_API_URL, Service.TOKEN)
    tasks = BackgroundTasks()
    try:
        # Walked by both processes against the one database: the steps are idempotent
        await init_db()
        bot = RemoteBot(client)
        if not await bot.sync():
            logger.warning('Бот пока не отвечает – игра считает, что эфира нет, до первого его запроса')
        tasks.start('api', lambda: serve(GameService(bot).app(), Service.ROLL_API_HOST, Service.ROLL_API_PORT,
                                         'API игры'))
        if Roll.OVERLAY_ENABLED:
            app = web.Application()
            overlay.add_routes(app)
            tasks.start('overlay', lambda: sse.serve(app, [overlay.feed], Roll.OVERLAY_HOST, Roll.OVERLAY_PORT))
        # A restart mid-stream: titles and regalia for the overlay, and the perks the bot's
        # stream start may have failed to deliver while the game was down; nothing is granted twice
        await on_stream(bot, bot.session_id, live=bot.stream_live)
        tasks.start('state', bot.state_loop)
        # Curses come from rewards and from the previous stream's results
        if Rewards.ENABLED or Roll.PERKS_ENABLED:
            tasks.start('curse_lift', lambda: curse_lift_loop(bot))
        # The choking flask comes only from a reward
        if Rewards.ENABLED:
            tasks.start('choke', lambda: choke_loop(bot))
        logger.info('Игра запущена | Сессия: %s | Эфир: %s', bot.session_id, 'идёт' if bot.stream_live else 'нет')
        await shutdown.wait()
    finally:
        await tasks.stop()
        await client.close()
        await close_db()
