"""State files under /data: JSON, each replaced whole."""

import json
from pathlib import Path


def read_json(path: Path, default):
    """A state file's JSON, or default when it is missing or unreadable."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Replace a state file atomically. A crash mid-write leaves the previous one."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)
