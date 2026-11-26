"""Hall lines to Twitch as the viewer, with the viewer's Twitch token, kept in memory for the session only."""

import time

from gate import config, state, twitch, upstream

TWITCH_MSG_MAX = 500


def remember(oc_id: str, user: dict, tok: dict) -> None:
    """Keep a viewer's Twitch token as the voice of their chat user. A previous token of theirs is revoked."""
    old = state.voices.get(oc_id)
    for other in [k for k, v in state.voices.items() if v["twitch_id"] == user["id"] and k != oc_id]:
        old = state.voices.pop(other)  # the same viewer under a previous Owncast user
    state.voices[oc_id] = {
        "twitch_id": user["id"],
        "login": user["login"],
        "access": tok["access_token"],
        "refresh": tok["refresh_token"],
        "expires": time.time() + int(tok.get("expires_in") or 3600),
    }
    if old:
        twitch.revoke_later([old["access"]])


def forget(oc_id: str) -> None:
    """Forget one chat user's voice and revoke its token with Twitch in the background."""
    gone = state.voices.pop(oc_id, None)
    if gone:
        twitch.revoke_later([gone["access"]])


def forget_voices() -> None:
    """Forget viewer tokens when a session ends and revoke them with Twitch in the background."""
    gone = [v["access"] for v in state.voices.values()]
    state.voices.clear()
    twitch.revoke_later(gone)


async def _refresh(v: dict) -> bool:
    status, tok = await upstream.http(
        "POST",
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": config.TWITCH_CLIENT_ID,
            "client_secret": config.TWITCH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": v["refresh"],
        },
    )
    if status != 200 or not tok or not tok.get("access_token"):
        return False
    v.update(
        access=tok["access_token"],
        refresh=tok.get("refresh_token") or v["refresh"],
        expires=time.time() + int(tok.get("expires_in") or 3600),
    )
    return True


def _drop(oc_id: str, why: str) -> None:
    state.voices.pop(oc_id, None)
    print(f"gate: hall→twitch: {why}, voice dropped", flush=True)


async def _send(v: dict, message: str) -> tuple[int, dict | list | None]:
    """Post the line with the viewer's token. A 401 refreshes the token and tries once more."""
    for retry in (False, True):
        status, data = await upstream.http(
            "POST",
            "https://api.twitch.tv/helix/chat/messages",
            json_body={"broadcaster_id": twitch.channel_id, "sender_id": v["twitch_id"], "message": message},
            headers=twitch.helix(v["access"]),
        )
        if status == 401 and not retry and await _refresh(v):
            continue
        break
    return status, data


def _refusal(status: int, data) -> str | None:
    """Why Twitch did not post the line, or None when it did."""
    sent = ((data or {}).get("data") or [{}])[0] if isinstance(data, dict) else {}
    if status == 200 and sent.get("is_sent"):
        return None
    reason = (sent.get("drop_reason") or {}).get("message") if sent else None
    return f"HTTP {status}, {reason or 'no reason given'}"


async def say(oc_id: str, text: str) -> None:
    """Send a hall line to Twitch chat as the viewer, prefixed with the mark.

    A Twitch refusal is logged. A token Twitch no longer accepts drops the voice.
    """
    v = state.voices.get(oc_id)
    if not v or not twitch.channel_id:
        return
    if v["expires"] - time.time() < 120 and not await _refresh(v):
        _drop(oc_id, f"token of {v['login']} not refreshed")
        return
    status, data = await _send(v, f"{config.HALL_MARK} {text}"[:TWITCH_MSG_MAX])
    if status == 401:
        _drop(oc_id, f"Twitch refuses the token of {v['login']}")
        return
    refusal = _refusal(status, data)
    if refusal:
        print(f"gate: hall→twitch: line of {v['login']} not accepted ({refusal})", flush=True)
