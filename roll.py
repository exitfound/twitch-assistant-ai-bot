"""Entry point of the game service (src/roll/): the «залупа стрима» game beside the bot."""
import asyncio
import contextlib

from src.roll.main import run

if __name__ == '__main__':
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
