"""The bot's background loops by name: started once, stopped together."""
import asyncio
import logging
from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)


class BackgroundTasks:

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def start(self, name: str, loop: Callable[[], Coroutine]) -> bool:
        """Start the loop unless it is already running. True – started now.

        event_ready fires again on every reconnect: a second copy of a loop would
        double every announcement.
        """
        if self.running(name):
            return False
        self._tasks[name] = asyncio.create_task(loop(), name=name)
        return True

    async def stop(self) -> None:
        """Cancel the loops and wait for them, before the database is closed.

        A loop waking up after close_db() calls get_db(), which opens a fresh connection
        and with it a new non-daemon aiosqlite thread, and the process never exits.
        close() runs more than once on shutdown: the later calls find nothing left.
        """
        tasks = [t for t in self._tasks.values() if not t.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning('Фоновая задача завершилась с ошибкой: %r', result)
        logger.info('Фоновые задачи остановлены: %d', len(tasks))
