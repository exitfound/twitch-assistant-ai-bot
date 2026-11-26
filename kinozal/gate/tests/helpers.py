"""Helpers of the gate's tests: the test environment's constants, Owncast markers, requests as the bot and a viewer."""

import asyncio
import base64
import html
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.sync.client import connect

OWNCAST = os.environ.get("OWNCAST_URL", "")
OWNCAST_PASSWORD = os.environ.get("OWNCAST_ADMIN_PASSWORD", "")
TOKEN = "bot-token"
CHANNEL_ID = "100"


def _owncast_live() -> bool:
    try:
        with urllib.request.urlopen(f"{OWNCAST}/api/status", timeout=3) as r:
            return bool(json.loads(r.read()).get("online"))
    except (OSError, ValueError):
        return False


needs_owncast = pytest.mark.skipif(not OWNCAST, reason="no local Owncast (OWNCAST_URL)")
# Owncast drops viewers' lines while offline. Tests where a viewer writes need a live stream
needs_stream = pytest.mark.skipif(
    not (OWNCAST and _owncast_live()), reason="no stream in the local Owncast: it drops viewers' lines"
)


def fresh_gate():
    """The gate package imported anew, as at a gate start: it reads the environment and its state files."""
    for name in [m for m in sys.modules if m == "gate" or m.startswith("gate.")]:
        del sys.modules[name]
    import gate.main

    return gate


def set_cookie(r, name: str) -> str:
    """The response's Set-Cookie header for this cookie, lowercased."""
    return next(h for h in r.headers.get_list("set-cookie") if h.startswith(f"{name}=")).lower()


def bot(client, method, **kw):
    return client.request(method, "/_gate/api/session", headers={"Authorization": f"Bearer {TOKEN}"}, **kw)


def login(client, nxt="/"):
    """Sign in with Twitch, then enter the session code. Returns the code response or the callback refusal."""
    r = twitch_step(client, nxt)
    if r.status_code != 303 or not r.headers["location"].startswith("/_gate/enter"):
        return r
    code = bot(client, "GET").json()["code"]
    return client.post("/_gate/enter", data={"code": code, "next": nxt})


def twitch_step(client, nxt="/"):
    """Go through Twitch and back with an authorization code. Returns the callback response."""
    r = client.get(f"/_gate/twitch?next={nxt}")
    assert r.status_code == 303, r.text
    to = urlparse(r.headers["location"])
    assert to.netloc == "id.twitch.tv"
    q = parse_qs(to.query)
    assert q["redirect_uri"] == ["https://kz.example.org/_gate/twitch/callback"]
    assert q["scope"] == ["user:read:follows user:read:subscriptions user:write:chat"]
    return client.get(f"/_gate/twitch/callback?code=good-code&state={q['state'][0]}")


def handoff(text: str) -> dict:
    """The handoff page's data attributes, unescaped: {"token": …, "next": …}."""
    return {k: html.unescape(v) for k, v in re.findall(r'data-(token|next)="([^"]*)"', text)}


def handoff_token(r) -> str:
    return handoff(r.text)["token"]


def static(client, page_text: str, name: str) -> str:
    """A gate file the page links, fetched the way the browser does."""
    url = re.search(rf'(?:src|href)="(/_gate/static/[^"]+/{re.escape(name)})"', page_text).group(1)
    r = client.get(url)
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    return r.text


def can_watch(client) -> bool:
    return client.get("/_gate/auth").status_code == 200


def client_address(gate) -> str:
    return gate.security.client_ip(
        type("Req", (), {"headers": {}, "client": type("C", (), {"host": "testclient"})()})()
    )


def names(gate) -> set:
    return {c["name"] for c in gate.state.chatters.values()}


def chat(client, method="POST", **kw):
    return client.request(method, "/_gate/api/chat", headers={"Authorization": f"Bearer {TOKEN}"}, **kw)


def say(token: str, *texts: str, rename: str | None = None) -> None:
    """A viewer writes to the Owncast chat over the websocket, like a browser."""
    with connect(f"{OWNCAST.replace('http', 'ws')}/ws?accessToken={token}") as ws:
        time.sleep(0.3)
        if rename:
            ws.send(json.dumps({"type": "NAME_CHANGE", "newName": rename}))
            time.sleep(1.1)
        for text in texts:
            ws.send(json.dumps({"type": "CHAT", "body": text}))
            time.sleep(1.1)  # Owncast flood protection


def anonymous_token() -> str:
    return json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{OWNCAST}/api/chat/register",
                method="POST",
                data=b'{"displayName":"anon"}',
                headers={"Content-Type": "application/json"},
            )
        ).read()
    )["accessToken"]


def hall_history(gate) -> list:
    return json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{OWNCAST}/api/integrations/chat", headers={"Authorization": f"Bearer {gate.owncast.chat_token}"}
            )
        ).read()
    )


def by(gate, name) -> list:
    asyncio.run(gate.owncast.get_chat_token())
    return [m for m in hall_history(gate) if (m.get("user") or {}).get("displayName") == name]


def admin(path, body=None):
    """Request to the real Owncast admin API."""
    basic = base64.b64encode(f"admin:{OWNCAST_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        f"{OWNCAST}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Authorization": "Basic " + basic, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read() or b"null")
