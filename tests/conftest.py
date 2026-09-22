"""Test harness: no .env, a temporary database per test, no Gemini, no Twitch.

The environment is set before anything from src is imported: config reads it at import
time, and python-dotenv would otherwise find the project's .env with live keys.
"""
import os

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ.update({
    'TWITCH_CLIENT_ID': 'test',
    'TWITCH_CLIENT_SECRET': 'test',
    'TWITCH_BOT_ID': '1000',
    'TWITCH_CHANNEL': 'testchannel',
    'GEMINI_API_KEY': 'test',
    # Nothing may reach the live database: a test without the db fixture fails loudly
    'BOT_DB_PATH': '/nonexistent/chat_history.db',
})

import asyncio
import collections

import pytest

from fakes import FakeBot
from src.core import content, database
from src.core.db import connection
from src.core.db import knowledge as db_knowledge
from src.core.config import Gemini
from src.gemini import client, commands
from src.gemini.memory import build
from src.gemini.picture import command as picture_command
from src.local import help_announce
from src.local.roll import game, perks

def content_text() -> str:
    """A CONTENT.md with every required key: the value is the key's own name, so a test
    sees which text was chosen without depending on the wording of the real file."""
    parts = []
    for section, keys in content.REQUIRED.items():
        parts.append(f'## {section}')
        for key in keys:
            value = '' if section == 'lists' else f'{section}.{key}'
            if (section, key) == ('lists', 'follow'):
                value = 'follow {user}'
            parts += [f'### {key}', value, '']
    return '\n'.join(parts)


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    """Fresh module state for every test.

    asyncio primitives bind to the first event loop that waits on them, and pytest-asyncio
    gives every test its own loop, so the module-level ones are recreated. A forgotten
    patch of generate() must not turn into a bill: the Gemini client refuses to exist.
    """
    monkeypatch.setattr(connection, '_db', None)
    monkeypatch.setattr(connection, '_closed', False)
    monkeypatch.setattr(connection, '_db_lock', asyncio.Lock())
    monkeypatch.setattr(connection, '_write_lock', asyncio.Lock())
    monkeypatch.setattr(db_knowledge, '_knowledge_ids', None)
    monkeypatch.setattr(client, '_semaphore', asyncio.Semaphore(Gemini.CONCURRENCY))
    monkeypatch.setattr(game, '_lock', asyncio.Lock())
    monkeypatch.setattr(build, '_slots', asyncio.Semaphore(build.MEMORY_CONCURRENCY))
    monkeypatch.setattr(build, '_lock', asyncio.Lock())
    for limit in [*commands.LIMITS.values(), picture_command.LIMIT]:
        monkeypatch.setattr(limit, 'busy', set())
    monkeypatch.setattr(picture_command, '_cache', collections.OrderedDict())
    monkeypatch.setattr(perks, '_pending', {})
    monkeypatch.setattr(help_announce, '_help_shown_at', 0.0)
    monkeypatch.setattr(client, 'usage', {'prompt': 0, 'cached': 0, 'output': 0})

    def no_gemini():
        raise AssertionError('A test reached the real Gemini client: patch generate() where it is used')
    monkeypatch.setattr(client, 'get_client', no_gemini)

    path = tmp_path / 'CONTENT.md'
    path.write_text(content_text(), encoding='utf-8')
    monkeypatch.setattr(content, 'CONTENT_PATH', path)
    monkeypatch.setattr(content, '_content', content._ContentFile(path))


@pytest.fixture
async def db(monkeypatch, tmp_path):
    """An empty database with the full schema, closed after the test."""
    monkeypatch.setattr(connection, 'DB_PATH', tmp_path / 'test.db')
    await database.init_db()
    yield await database.get_db()
    await database.close_db()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()
