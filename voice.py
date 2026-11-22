"""Entry point of the voice service (src/discord/): the bot's answers spoken in Discord."""
import asyncio
import contextlib

from src.discord.main import run

if __name__ == '__main__':
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
