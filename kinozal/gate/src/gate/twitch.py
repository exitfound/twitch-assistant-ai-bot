"""Twitch's API: the viewer and the channel, follows and subscriptions, the app token, revoked tokens."""

import threading
import time
from contextlib import suppress
from urllib.parse import quote

from gate import config, upstream

channel_id: str | None = None  # the channel's Twitch id, looked up once
app_token: dict = {"token": None, "until": 0.0}


def helix(token: str) -> dict:
    """Headers of a Helix call made with this token."""
    return {"Authorization": f"Bearer {token}", "Client-Id": config.TWITCH_CLIENT_ID}


async def find_channel_id(hdrs: dict) -> str:
    """The channel's Twitch id, looked up by login once."""
    global channel_id
    if channel_id is None:
        status, ch = await upstream.http(
            "GET", f"https://api.twitch.tv/helix/users?login={quote(config.TWITCH_CHANNEL)}", headers=hdrs
        )
        if status != 200 or not ch or not ch.get("data"):
            raise upstream.Upstream(f"helix/users?login: HTTP {status}")
        channel_id = ch["data"][0]["id"]
    return channel_id


async def twitch_user(token: str) -> tuple[dict, str]:
    """The viewer by their token, and the channel id."""
    status, me = await upstream.http("GET", "https://api.twitch.tv/helix/users", headers=helix(token))
    if status != 200 or not me or not me.get("data"):
        raise upstream.Upstream(f"helix/users: HTTP {status}")
    return me["data"][0], await find_channel_id(helix(token))


async def may_watch(token: str, user_id: str, chan: str) -> bool:
    """Whether the viewer is the streamer, a follower or a subscriber. Checked with the viewer's own token."""
    if user_id == chan:
        return True
    hdrs = helix(token)
    status, follows = await upstream.http(
        "GET", f"https://api.twitch.tv/helix/channels/followed?user_id={user_id}&broadcaster_id={chan}", headers=hdrs
    )
    if status != 200:
        raise upstream.Upstream(f"helix/channels/followed: HTTP {status}")
    if follows and follows.get("data"):
        return True
    status, _ = await upstream.http(
        "GET", f"https://api.twitch.tv/helix/subscriptions/user?broadcaster_id={chan}&user_id={user_id}", headers=hdrs
    )
    if status == 200:
        return True
    if status == 404:
        return False
    raise upstream.Upstream(f"helix/subscriptions/user: HTTP {status}")


async def revoke(token: str) -> None:
    """Revoke a viewer token that is no longer needed. A failure does not block sign-in."""
    with suppress(upstream.Upstream):
        await upstream.http(
            "POST", "https://id.twitch.tv/oauth2/revoke", data={"client_id": config.TWITCH_CLIENT_ID, "token": token}
        )


def revoke_later(tokens: list[str]) -> None:
    """Revoke tokens in a background thread. Called where the caller must not wait, a session's end included."""

    def run():
        for t in tokens:
            with suppress(upstream.Upstream):
                upstream.http_sync(
                    "POST",
                    "https://id.twitch.tv/oauth2/revoke",
                    data={"client_id": config.TWITCH_CLIENT_ID, "token": t},
                )

    if tokens:
        threading.Thread(target=run, daemon=True).start()


async def app_access_token() -> str:
    """Twitch app token (client credentials) for Helix calls without a viewer."""
    if app_token["token"] and time.time() < app_token["until"]:
        return app_token["token"]
    status, tok = await upstream.http(
        "POST",
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": config.TWITCH_CLIENT_ID,
            "client_secret": config.TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    if status != 200 or not tok or not tok.get("access_token"):
        raise upstream.Upstream(f"oauth2/token (app): HTTP {status}")
    app_token.update(token=tok["access_token"], until=time.time() + int(tok.get("expires_in") or 3600) - 300)
    return app_token["token"]
