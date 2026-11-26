"""The viewer's way in and out: Caddy's check, the entry page and its code, the language, the streamer's
dock pass, the session for the page and the OBS placeholder, logout and the entry page's static files."""

import html
import time
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from gate import access, config, pages, security, session, state, texts, voices
from gate.routers import body

router = APIRouter(prefix=config.P)


@router.get("/auth")
async def auth(req: Request):
    """Caddy's forward_auth: a signed-in viewer passes, a page request goes to the entry page."""
    if access.host_ok(req) or access.cookie_ok(req):
        return Response(status_code=200)
    if req.headers.get("x-forwarded-method", "GET") == "GET":
        nxt = security.safe_next(req.headers.get("x-forwarded-uri", "/"))
        return RedirectResponse(f"{config.P}/enter?next={quote(nxt)}", status_code=302)
    return Response(status_code=401)


@router.get("/live")
async def live(req: Request):
    """Session start, film title and gate time for the block under the player (customjavascript).

    Signed-in viewers only. The gate's time keeps the timer right when the viewer's clock is off.
    """
    if not (access.host_ok(req) or access.cookie_ok(req)):
        return Response(status_code=401)
    s = state.session
    return JSONResponse(
        {
            "started": s["started"] if s else None,
            "title": s.get("title") if s else None,
            "now": time.time(),
            "voice": viewer_voice(req),
        },
        headers={"Cache-Control": "no-store"},
    )


def viewer_voice(req: Request) -> bool | None:
    """Whether the viewer's hall lines go to Twitch. None without a Twitch sign-in (code only, OBS dock).

    Viewer tokens live in memory only. After a gate restart the voice is gone until the viewer passes
    Twitch again, and custom.js shows a "Reconnect" button for that (no code needed).
    """
    if not config.TWITCH_ENABLED or not access.cookie_ok(req):
        return None
    record = access.cookie_chatter(req)
    return None if not record else record.get("oc_id") in state.voices


@router.get("/now")
async def now_showing():
    """Session state, title and start for the placeholder in OBS.

    No sign-in, any origin. The entry page shows the same to everyone, and the answer carries
    no code or stream links.
    """
    s = state.session
    return JSONResponse(
        {
            "open": bool(s),
            "title": s.get("title") if s else None,
            "started": s["started"] if s else None,
            "now": time.time(),
        },
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/lang")
async def set_lang(to: str = "", next: str = "/"):
    """Language switch of the entry page. Saves the choice for the gate and custom.js, then goes back."""
    resp = RedirectResponse(f"{config.P}/enter?next={quote(security.safe_next(next))}", status_code=303)
    lang = to if to in texts.LANGS else texts.LANGS[0]
    resp.set_cookie(config.LANG_COOKIE, lang, max_age=config.LANG_TTL, secure=True, samesite="lax", path="/")
    return resp


@router.get("/enter")
async def enter_form(req: Request, next: str = "/"):
    nxt, lang = security.safe_next(next), pages.lang_of(req)
    session.expire()
    if not state.session:
        return pages.page(lang, closed=True)
    if access.cookie_ok(req):
        return RedirectResponse(pages.with_lang(nxt, lang), status_code=303)
    who = access.pending_user(req)
    return pages.page(lang, nxt=nxt, who=who["name"] if who else None)


def _code_refused(req: Request, code: str, account: str | None) -> tuple[str, int] | None:
    """Why the code is refused, as the text key and the status, or None.

    Counted per Twitch account, so one viewer's typos do not lock out others behind the same address.
    """
    ip = security.client_ip(req)
    limit = security.FAIL_IP_LIMIT if account else security.FAIL_LIMIT
    if security.throttled(ip, limit) or (account and security.throttled(account)):
        return "too_many", 429
    if not security.same(code, state.session["code"]):
        security.failed(ip, *([account] if account else []))
        return "bad_code", 401
    return None


@router.post("/enter")
async def enter_submit(req: Request):
    form = parse_qs((await req.body()).decode(errors="replace"))
    code = form.get("code", [""])[0].strip()
    nxt, lang = security.safe_next(form.get("next", ["/"])[0]), pages.lang_of(req)
    session.expire()
    if not state.session:
        return pages.page(lang, closed=True)

    who = access.pending_user(req)
    if config.TWITCH_ENABLED and not who:
        return pages.page(lang, nxt=nxt, error=texts.tr(lang, "need_twitch"), status=401)
    twitch_id = access.pending_id(req) if who else None
    refused = _code_refused(req, code, f"tw:{twitch_id}" if twitch_id else None)
    if refused:
        key, status = refused
        return pages.page(lang, nxt=nxt, who=who and who["name"], error=texts.tr(lang, key), status=status)

    # With Twitch the viewer chats under the Twitch name. Without it, anonymously
    site = pages.with_lang(nxt, lang)
    resp = pages.chat_handoff(who["oc_token"], site, lang) if who else RedirectResponse(site, status_code=303)
    access.set_entry_cookie(resp, twitch_id)
    resp.delete_cookie(config.PENDING_COOKIE, path=config.P)
    return resp


@router.get("/host")
async def host_login(req: Request, key: str = "", next: str = "/embed/chat/readwrite/"):
    """The streamer's permanent entry for the OBS chat dock."""
    ip = security.client_ip(req)
    if security.throttled(ip):
        return Response(status_code=429)
    if not security.same(key, security.HOST_KEY):
        security.failed(ip)
        return Response(status_code=404)
    # after a Twitch sign-in by the streamer the dock writes under their nick with moderator rights
    own, lang = access.streamer_chatter(), pages.lang_of(req)
    site = pages.with_lang(security.safe_next(next), lang)
    resp = pages.chat_handoff(own["oc_token"], site, lang) if own else RedirectResponse(site, status_code=303)
    resp.set_cookie(
        config.HOST_COOKIE,
        security.HOST_KEY,
        max_age=config.HOST_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/logout")
async def logout_page(req: Request):
    """Target of the "Log out" button in custom.js. Clears the entry cookies and localStorage.

    The page POSTs the chat token first, so the gate revokes this viewer's Twitch token,
    then opens the entry page.
    """
    body = (
        f'<script src="{pages.STATIC_URL}/logout.js"></script>'
        f'<noscript><a href="/_gate/enter">{html.escape(texts.tr(pages.lang_of(req), "to_entry"))}</a></noscript>'
    )
    resp = pages.html_page(f'<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">{body}')
    resp.delete_cookie(config.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    resp.delete_cookie(config.PENDING_COOKIE, path=config.P, secure=True, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout(req: Request):
    """Forget the voice of the viewer with this {"token": Owncast chat token} and revoke their Twitch token."""
    token = (await body.json_object(req) or {}).get("token")
    if isinstance(token, str) and token:
        for record in state.chatters.values():
            if security.same(record.get("oc_token", ""), token):
                voices.forget(record["oc_id"])
                break
    return Response(status_code=204)


@router.get("/static/{version}/{name}")
async def static_file(version: str, name: str):
    """The entry page's CSS and scripts. Any version gets the current file, the page always links the current one."""
    item = pages.STATIC.get(name)
    if item is None:
        return Response(status_code=404)
    return Response(item[0], media_type=item[1], headers={"Cache-Control": "public, max-age=31536000, immutable"})
