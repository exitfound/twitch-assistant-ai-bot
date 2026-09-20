"""Where the bot's files live: the database and CONTENT.md.

Both used to be computed from `Path(__file__).parents[2]`, which pins them to the
repository. That is fine for a checkout and wrong for a container, where the code
is immutable and the data has to sit on a volume. The environment can now move
them; an empty environment keeps the old paths byte for byte.

Importing config first matters: it calls load_dotenv(), so .env is applied before
the paths below are computed (2026-09-20).
"""
import os
from pathlib import Path

from src.core import config  # noqa: F401 – imported for its load_dotenv() side effect

# Project root: src/core/paths.py → three levels up
ROOT = Path(__file__).resolve().parents[2]

DB_PATH = Path(os.getenv('BOT_DB_PATH') or ROOT / 'chat_history.db').expanduser()
CONTENT_PATH = Path(os.getenv('BOT_CONTENT_PATH') or ROOT / 'CONTENT.md').expanduser()
