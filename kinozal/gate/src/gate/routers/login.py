"""The Twitch sign-in: off to Twitch's consent screen and back, the account checked, its chat user and voice."""

import base64
import secrets
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from gate import (
    access,
    config,
    look,
    moderation,
    owncast,
    pages,
    security,
    session,
    state,
    texts,
    twitch,
    upstream,
    voices,
)

router = APIRouter(prefix=config.P)

OAUTH_COOKIE = "kzt"  # the OAuth state and the return address, signed
OAUTH_TTL = 600  # time for the Twitch consent screen
SCOPES = "user:read:follows user:read:subscriptions user:write:chat"


def _callback_url() -> str:
    return f"{config.PUBLIC_URL}{config.P}/twitch/callback"


def oauth_cookie(oauth_state: str, nxt: str) -> str:
    raw = f"{oauth_state}|{nxt}"
    return base64.urlsafe_b64encode(raw.encode()).decode() + "." + security.sign(raw)


def read_oauth_cookie(value: str) -> tuple[str, str] | None:
    b64, _, sig = value.partition(".")
    try:
        raw = base64.urlsafe_b64decode(b64.encode()).decode()
    except ValueError:
        return None
    if not security.same(sig, security.sign(raw)):
        return None
    oauth_state, _, nxt = raw.partition("|")
    return oauth_state, nxt


@router.get("/twitch")
async def twitch_login(req: Request, next: str = "/"):
    nxt = security.safe_next(next)
    session.expire()
    if not config.TWITCH_ENABLED:
        return RedirectResponse(f"{config.P}/enter?next={quote(nxt)}", status_code=303)
    if not state.session:
        return pages.page(pages.lang_of(req), closed=True)
    oauth_state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": config.TWITCH_CLIENT_ID,
            "redirect_uri": _callback_url(),
            "response_type": "code",
            "scope": SCOPES,
            "state": oauth_state,
        }
    )
    resp = RedirectResponse(f"https://id.twitch.tv/oauth2/authorize?{query}", status_code=303)
    resp.set_cookie(
        OAUTH_COOKIE,
        oauth_cookie(oauth_state, nxt),
        max_age=OAUTH_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path=config.P,
    )
    return resp


class _Refused(Exception):
    """A sign-in refused with this entry page text and status."""

    def __init__(self, key: str, status: int = 400):
        super().__init__(key)
        self.key, self.status = key, status


def _fail(lang: str, nxt: str, key: str, status: int = 400) -> HTMLResponse:
    resp = pages.page(lang, nxt=nxt, error=texts.tr(lang, key, chan=config.TWITCH_CHANNEL), status=status)
    resp.delete_cookie(OAUTH_COOKIE, path=config.P)
    return resp


def _before_sign_in(lang: str, nxt: str, saved, code: str, oauth_state: str, error: str) -> Response | None:
    """The answer when the callback cannot go on to Twitch: no app, no session, cancelled, or a forged state."""
    session.expire()
    if not config.TWITCH_ENABLED:
        return RedirectResponse(f"{config.P}/enter", status_code=303)
    if not state.session:
        return pages.page(lang, closed=True)
    if error:
        return _fail(lang, nxt, "twitch_cancelled")
    if not saved or not code or not security.same(oauth_state, saved[0]):
        return _fail(lang, nxt, "twitch_failed")
    return None


async def _token(code: str) -> dict:
    """The viewer's tokens for the authorization code."""
    status, tok = await upstream.http(
        "POST",
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": config.TWITCH_CLIENT_ID,
            "client_secret": config.TWITCH_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _callback_url(),
        },
    )
    if status != 200 or not tok or not tok.get("access_token"):
        raise _Refused("twitch_failed")
    return tok


async def _admit(token: str) -> tuple[dict, dict]:
    """Check the account and give it its chat user with its Twitch colour: (Twitch user, chatter)."""
    user, chan = await twitch.twitch_user(token)
    if moderation.banned(user["id"], user["login"]):
        raise _Refused("banned", 403)
    if not await twitch.may_watch(token, user["id"], chan):
        raise _Refused("not_follower", 403)
    chatter = await owncast.chat_identity(user)
    # the hall nick gets the viewer's Twitch colour; the streamer, the broadcaster badge
    first_badge = user["id"] == chan and chatter["name"].casefold() not in state.look["badges"]
    color = await look.viewer_color(token, user["id"])
    look.remember(chatter["name"], color, None, ["broadcaster/1"] if first_badge else None)
    return user, chatter


def _keep_voice(tok: dict, user: dict, chatter: dict, sid: str) -> bool:
    """Keep the token as the viewer's voice, only with write:chat and only for the session the sign-in began in.

    The awaits of the sign-in may outlast that session, and a token kept after forget_voices() would never be revoked.
    """
    may_write = "user:write:chat" in (tok.get("scope") or []) and tok.get("refresh_token")
    if may_write and state.session and state.session["sid"] == sid:
        voices.remember(chatter["oc_id"], user, tok)
        return True
    return False


def _signed_in(req: Request, user: dict, chatter: dict, nxt: str, lang: str) -> Response:
    if access.cookie_ok(req):
        # Code entered earlier, so straight to chat under the Twitch nick. The entry cookie is reissued
        # for this account. The old one may name another, and the ban check reads it
        resp = pages.chat_handoff(chatter["oc_token"], pages.with_lang(nxt, lang), lang)
        access.set_entry_cookie(resp, user["id"])
        resp.delete_cookie(config.PENDING_COOKIE, path=config.P, secure=True, httponly=True, samesite="lax")
    else:
        # Twitch passed, the code is still required
        resp = RedirectResponse(f"{config.P}/enter?next={quote(nxt)}", status_code=303)
        resp.set_cookie(
            config.PENDING_COOKIE,
            f"{user['id']}.{access.pending_sign(user['id'])}",
            max_age=config.PENDING_TTL,
            httponly=True,
            secure=True,
            samesite="lax",
            path=config.P,
        )
    resp.delete_cookie(OAUTH_COOKIE, path=config.P)
    return resp


@router.get("/twitch/callback")
async def twitch_callback(req: Request, code: str = "", oauth_state: str = Query("", alias="state"), error: str = ""):
    saved = read_oauth_cookie(req.cookies.get(OAUTH_COOKIE, ""))
    nxt, lang = security.safe_next(saved[1]) if saved else "/", pages.lang_of(req)
    early = _before_sign_in(lang, nxt, saved, code, oauth_state, error)
    if early is not None:
        return early
    sid, tok, kept = state.session["sid"], None, False
    try:
        tok = await _token(code)
        user, chatter = await _admit(tok["access_token"])
        kept = _keep_voice(tok, user, chatter, sid)
    except _Refused as e:
        return _fail(lang, nxt, e.key, e.status)
    except upstream.Upstream as e:
        print(f"gate: login: {e}", flush=True)
        return _fail(lang, nxt, "unavailable", status=502)
    finally:
        if tok and not kept:
            await twitch.revoke(tok["access_token"])
    if not state.session or state.session["sid"] != sid:
        resp = pages.page(lang, closed=True)
        resp.delete_cookie(OAUTH_COOKIE, path=config.P)
        return resp
    return _signed_in(req, user, chatter, nxt, lang)
