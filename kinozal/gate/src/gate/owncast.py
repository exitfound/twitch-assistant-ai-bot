"""Owncast's APIs: admin calls, integration tokens, the chat users of Twitch viewers, the chat list, a nick reset."""

import base64
import json
import time
from urllib.parse import quote, urlencode, urlparse, urlunparse

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from gate import config, state, upstream

CHAT_BOT_NAME = "Twitch"  # Twitch lines appear under this name when a twin cannot post
CHAT_SCOPES = ["CAN_SEND_MESSAGES", "HAS_ADMIN_ACCESS"]  # chat history requires HAS_ADMIN_ACCESS

chat_token: str | None = None  # the «Twitch» integration token, cached


def _admin_headers() -> dict:
    raw = f"admin:{config.OWNCAST_ADMIN_PASSWORD}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def admin(method: str, path: str, json_body: dict | None = None):
    """A call to Owncast's admin API inside the compose network: (status, JSON or None)."""
    return await upstream.http(method, f"{config.OWNCAST_URL}{path}", headers=_admin_headers(), json_body=json_body)


def _holds(token: dict, name: str, scopes: list[str]) -> bool:
    return token.get("displayName") == name and set(scopes) <= set(token.get("scopes") or [])


async def integration_token(name: str, scopes: list[str]) -> str:
    """An Owncast integration token with this name and every one of these scopes, found or created."""
    status, tokens = await admin("GET", "/api/admin/accesstokens")
    if status != 200 or not isinstance(tokens, list):
        raise upstream.Upstream(f"owncast accesstokens: HTTP {status}")
    found = next((t["accessToken"] for t in tokens if _holds(t, name, scopes)), None)
    if found:
        return found
    status, t = await admin("POST", "/api/admin/accesstokens/create", {"name": name, "scopes": scopes})
    if status != 200 or not t or not t.get("accessToken"):
        raise upstream.Upstream(f"owncast accesstokens/create: HTTP {status}")
    return t["accessToken"]


async def get_chat_token(fresh: bool = False) -> str:
    """The «Twitch» integration token, from the cache unless fresh."""
    global chat_token
    if not chat_token or fresh:
        chat_token = await integration_token(CHAT_BOT_NAME, CHAT_SCOPES)
    return chat_token


async def integration(method: str, path: str, json_body: dict | None = None):
    """Request to the Owncast integrations API. On a 401 the token is fetched again, once."""
    for fresh in (False, True):
        token = await get_chat_token(fresh)
        status, data = await upstream.http(
            method, f"{config.OWNCAST_URL}{path}", json_body=json_body, headers={"Authorization": f"Bearer {token}"}
        )
        if status != 401:
            break
    if status != 200:
        raise upstream.Upstream(f"owncast {path}: HTTP {status}")
    return data


async def make_moderator(oc_id: str) -> None:
    """Make an Owncast chat user a moderator who can delete messages and ban. For the streamer only."""
    status, _ = await admin("POST", "/api/admin/chat/users/setmoderator", {"userId": oc_id, "isModerator": True})
    if status != 200:
        raise upstream.Upstream(f"owncast setmoderator: HTTP {status}")


async def chat_identity(user: dict) -> dict:
    """Owncast chat user for a Twitch viewer, always with the Twitch name.

    Within a session the viewer keeps one user across devices and sign-ins. A new session gets a new
    user, so the hall name always matches Twitch. Owncast rejects the old token after a chat ban or a
    lost Owncast database. A banned user keeps their token, so they can watch but not write. A new
    user would undo the ban.
    """
    known = state.chatters.get(user["id"])
    if known and await _keeps(known):
        return known
    return await _register(user)


async def _keeps(known: dict) -> bool:
    """Whether a viewer keeps their chat user: its token works in this session, or Owncast holds it disabled."""
    status, _ = await upstream.http("GET", f"{config.OWNCAST_URL}/api/chat?accessToken={quote(known['oc_token'])}")
    if status == 200 and state.session and known.get("sid") == state.session["sid"]:
        return True
    if status not in (200, 401):
        raise upstream.Upstream(f"owncast /api/chat: HTTP {status}")
    return status == 401 and await _is_disabled(known["oc_id"])


async def _is_disabled(oc_id: str) -> bool:
    status, disabled = await admin("GET", "/api/admin/chat/users/disabled")
    if status != 200 or not isinstance(disabled, list):
        raise upstream.Upstream(f"owncast disabled users: HTTP {status}")
    return any(d.get("id") == oc_id for d in disabled)


def _hall_name(user: dict) -> str:
    """The Twitch display name, or the login when the display name is in another script."""
    name = user.get("display_name") or user["login"]
    return name if name.casefold() == user["login"].casefold() else user["login"]


async def _register(user: dict) -> dict:
    """A new Owncast chat user for the viewer, kept in chatters for this session. The streamer becomes a moderator."""
    name = _hall_name(user)
    status, reg = await upstream.http(
        "POST", f"{config.OWNCAST_URL}/api/chat/register", json_body={"displayName": name}
    )
    if status != 200 or not reg or not reg.get("accessToken"):
        raise upstream.Upstream(f"owncast /api/chat/register: HTTP {status}")
    if user["login"].casefold() == config.TWITCH_CHANNEL:
        await make_moderator(reg["id"])  # only the streamer deletes messages and bans
    record = {
        "login": user["login"],
        "name": name,
        "oc_id": reg["id"],
        "oc_token": reg["accessToken"],
        "sid": state.session["sid"] if state.session else None,
    }
    state.chatters[user["id"]] = record
    state.save_chatters()
    return record


def _chat_user(client) -> tuple[str, str, bool] | None:
    """A chat client's user as (key, nick, moderator); None for a bot or a malformed entry."""
    u = client.get("user") if isinstance(client, dict) else None
    if not isinstance(u, dict) or u.get("isBot") or not isinstance(u.get("displayName"), str):
        return None
    mod = "MODERATOR" in (u.get("scopes") or []) or bool(u.get("isModerator"))
    return u.get("id") or u["displayName"], u["displayName"], mod


def chat_users(clients) -> list[dict]:
    """Users connected to Owncast chat, one entry per user across tabs, bots excluded."""
    seen: dict[str, dict] = {}
    for found in map(_chat_user, clients if isinstance(clients, list) else []):
        if not found:
            continue
        key, name, mod = found
        if key in seen:
            seen[key]["mod"] = seen[key]["mod"] or mod
        else:
            seen[key] = {"name": name, "mod": mod}
    return sorted(seen.values(), key=lambda x: x["name"].casefold())


def set_name(oc_token: str, name: str) -> None:
    """Rename an Owncast user with their own token, by one NAME_CHANGE message over the chat websocket. Blocking."""
    url = urlparse(config.OWNCAST_URL)
    ws_url = urlunparse(
        url._replace(
            scheme="wss" if url.scheme == "https" else "ws", path="/ws", query=urlencode({"accessToken": oc_token})
        )
    )
    try:
        with ws_connect(ws_url, open_timeout=config.HTTP_TIMEOUT, close_timeout=config.HTTP_TIMEOUT, proxy=None) as ws:
            time.sleep(1.5)  # Owncast rejects messages right after connecting (flood protection)
            ws.send(json.dumps({"type": "NAME_CHANGE", "newName": name}))
            # Closed right after this message, Owncast 0.3.0 stops taking chat lines from other viewers.
            # Half a second open keeps its chat working
            time.sleep(0.5)
    except (OSError, WebSocketException) as e:
        raise upstream.Upstream(f"owncast /ws: {e}") from e
