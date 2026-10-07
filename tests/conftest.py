"""Test harness: no .env, a temporary database per test, no Gemini, no Twitch.

The environment is set before anything from src is imported: config reads it at import
time, and python-dotenv would otherwise find the project's .env with live keys.
"""
import os
import re
from pathlib import Path

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
# Every variable the bot reads is dropped, so the tests see the defaults whatever the
# shell exported: with .env loaded into it, BOT_TIMEZONE=UTC or ROLL_MAX=50 failed tests.
# .env.example lists them all; a loop interval also takes the one-number PREFIX_MINUTES
_EXAMPLE = (Path(__file__).parents[1] / '.env.example').read_text(encoding='utf-8')
for _name in re.findall(r'^#?\s*([A-Z][A-Z0-9_]*)=', _EXAMPLE, flags=re.MULTILINE):
    os.environ.pop(_name, None)
    if _name.endswith('_MIN_MINUTES'):
        os.environ.pop(_name.removesuffix('_MIN_MINUTES') + '_MINUTES', None)
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
from src.core import content, database, speech
from src.core.db import chat as db_chat
from src.core.db import connection
from src.core.db import knowledge as db_knowledge
from src.core.config import Gemini
from src.gemini import client
from src.twitch.gemini import commands
from src.gemini.memory import build
from src.twitch.gemini import picture as picture_command
from src.twitch.local import clip, follow, help_announce
from src.twitch.local.mascot import feed as mascot_feed
from src.twitch.local.mascot import mood as mascot_mood
from src.twitch.local.roll import game, perks

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
    monkeypatch.setattr(db_chat, '_previous_sessions', {})
    monkeypatch.setattr(db_chat, '_last_sessions', {})
    monkeypatch.setattr(client, '_semaphore', asyncio.Semaphore(Gemini.CONCURRENCY))
    monkeypatch.setattr(game, '_lock', asyncio.Lock())
    monkeypatch.setattr(build, '_slots', asyncio.Semaphore(build.MEMORY_CONCURRENCY))
    monkeypatch.setattr(build, '_lock', asyncio.Lock())
    for limit in [*commands.LIMITS.values(), picture_command.LIMIT, clip.LIMIT]:
        monkeypatch.setattr(limit, 'busy', set())
    monkeypatch.setattr(picture_command, '_cache', collections.OrderedDict())
    monkeypatch.setattr(perks, '_pending', {})
    monkeypatch.setattr(help_announce, '_help_shown_at', 0.0)
    monkeypatch.setattr(follow, '_sent_at', collections.deque(maxlen=follow.GREETINGS_PER_MINUTE))
    monkeypatch.setattr(follow, '_greeted', set())
    monkeypatch.setattr(client, 'usage', {'prompt': 0, 'cached': 0, 'output': 0})
    monkeypatch.setattr(mascot_mood, 'tracker', mascot_mood.MoodTracker())
    monkeypatch.setattr(mascot_feed, 'tracker', mascot_mood.tracker)
    monkeypatch.setattr(mascot_feed, '_clients', set())
    monkeypatch.setattr(speech, '_listeners', [])

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
