"""Calls to Twitch and Owncast: one blocking HTTP request, run off the event loop by http()."""

import asyncio
import json
import urllib.error
import urllib.request
from http.client import HTTPException
from urllib.parse import urlencode

from gate import config


class Upstream(Exception):
    """Twitch or Owncast did not respond or responded unexpectedly."""


def http_sync(
    method: str,
    url: str,
    *,
    data: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | list | None]:
    """One request, returning (status, JSON or None). Network errors and timeouts raise Upstream."""
    body, hdrs = None, dict(headers or {})
    if data is not None:
        body = urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        try:
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
    # HTTPException covers a peer restarting mid-answer (IncompleteRead, RemoteDisconnected)
    except (urllib.error.URLError, HTTPException, TimeoutError, OSError) as e:
        raise Upstream(f"{method} {url.split('?')[0]}: {e}") from e
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


async def http(*args, **kwargs) -> tuple[int, dict | list | None]:
    """http_sync() in a worker thread, so a slow peer never blocks the event loop."""
    return await asyncio.to_thread(http_sync, *args, **kwargs)
