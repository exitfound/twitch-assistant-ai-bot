"""Where the bot's files live: the database and docs/CONTENT.md.

Both default to the repository (the database to its root), which suits a checkout but not
a container, where the code is immutable and the data has to sit on a volume, so the
environment can move them (BOT_DB_PATH, BOT_CONTENT_PATH, read in config).
"""
from pathlib import Path

from src.core.config import Files

# Project root: src/core/paths.py → three levels up
ROOT = Path(__file__).resolve().parents[2]

DB_PATH = Path(Files.DB or ROOT / 'chat_history.db').expanduser()
CONTENT_PATH = Path(Files.CONTENT or ROOT / 'docs' / 'CONTENT.md').expanduser()
