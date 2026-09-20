"""Where the bot's files live: the database and CONTENT.md.

Both default to the repository root, which suits a checkout but not a container, where
the code is immutable and the data has to sit on a volume, so the environment can move
them. config is imported first for its load_dotenv(), so .env is applied before the
paths below are computed.
"""
import os
from pathlib import Path

from src.core import config  # noqa: F401 – imported for its load_dotenv() side effect

# Project root: src/core/paths.py → three levels up
ROOT = Path(__file__).resolve().parents[2]

DB_PATH = Path(os.getenv('BOT_DB_PATH') or ROOT / 'chat_history.db').expanduser()
CONTENT_PATH = Path(os.getenv('BOT_CONTENT_PATH') or ROOT / 'CONTENT.md').expanduser()
