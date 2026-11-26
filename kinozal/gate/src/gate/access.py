"""Who may pass: the entry cookie, the Twitch step before the code, the streamer and the bot."""

from fastapi import Request, Response

from gate import config, moderation, security, session, state


def host_ok(req: Request) -> bool:
    """The streamer's permanent dock pass."""
    return security.same(req.cookies.get(config.HOST_COOKIE, ""), security.HOST_KEY)


def api_ok(req: Request) -> bool:
    """The bot's Bearer token."""
    return security.same(req.headers.get("authorization", ""), f"Bearer {config.API_TOKEN}")


def _who_sign(twitch_id: str, sid: str) -> str:
    return security.sign(f"who:{twitch_id}:{sid}")


def cookie_value(twitch_id: str | None) -> str:
    """Entry cookie. It names the session and the Twitch account, if any, so a ban invalidates it."""
    sid = state.session["sid"]
    value = f"{sid}.{security.sign(sid)}"
    return f"{value}.{twitch_id}.{_who_sign(twitch_id, sid)}" if twitch_id else value


def set_entry_cookie(resp: Response, twitch_id: str | None) -> None:
    resp.set_cookie(
        config.COOKIE,
        cookie_value(twitch_id),
        max_age=config.COOKIE_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def cookie_ok(req: Request) -> bool:
    """A valid entry cookie of the open session, its Twitch account not banned."""
    session.expire()
    if not state.session:
        return False
    parts = req.cookies.get(config.COOKIE, "").split(".")
    if len(parts) not in (2, 4):
        return False
    sid, sig = parts[0], parts[1]
    if not (security.same(sid, state.session["sid"]) and security.same(sig, security.sign(sid))):
        return False
    if len(parts) == 4:
        twitch_id, wsig = parts[2], parts[3]
        if not security.same(wsig, _who_sign(twitch_id, sid)) or moderation.banned(twitch_id):
            return False
    return True


def cookie_chatter(req: Request) -> dict | None:
    """The chat user of the Twitch account an entry cookie names. Only after cookie_ok() has verified it."""
    parts = req.cookies.get(config.COOKIE, "").split(".")
    return state.chatters.get(parts[2]) if len(parts) == 4 else None


def pending_sign(twitch_id: str) -> str:
    return security.sign(f"pending:{twitch_id}:{state.session['sid']}")


def pending_id(req: Request) -> str | None:
    """Twitch id of a viewer who passed Twitch in this session and has not entered the code yet."""
    if not state.session:
        return None
    twitch_id, _, sig = req.cookies.get(config.PENDING_COOKIE, "").partition(".")
    if not twitch_id or not security.same(sig, pending_sign(twitch_id)):
        return None
    return twitch_id


def pending_user(req: Request) -> dict | None:
    """The pending viewer's record in chatters."""
    twitch_id = pending_id(req)
    return state.chatters.get(twitch_id) if twitch_id else None


def streamer_chatter() -> dict | None:
    """The streamer's chat user after a Twitch sign-in. The dock's permanent entry uses it too."""
    owner = config.TWITCH_CHANNEL
    return next((r for r in state.chatters.values() if str(r.get("login", "")).casefold() == owner), None)


def streamer_ok(req: Request) -> bool:
    """Whether the request is the streamer's, by the dock's pass or a Twitch sign-in as the channel."""
    if host_ok(req):
        return True
    if not cookie_ok(req):
        return False
    record = cookie_chatter(req)
    return bool(record) and str(record.get("login", "")).casefold() == config.TWITCH_CHANNEL
