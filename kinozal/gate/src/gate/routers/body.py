"""Request bodies the gate takes as JSON objects: the bot's calls and custom.js's."""

import json

from fastapi import Request


async def json_object(req: Request) -> dict | None:
    """The body as a JSON object: {} when empty, None when it is not a JSON object."""
    raw = await req.body()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
