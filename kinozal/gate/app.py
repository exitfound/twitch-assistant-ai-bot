"""gate: the cinema hall's entry, session API and chat bridge (docs/KINOZAL.md).

Caddy asks /_gate/auth before every request to Owncast. A viewer enters through Twitch (followers
and subscribers of TWITCH_CHANNEL) and the session's code; the bot opens, renames and closes the
session through /_gate/api/session, and its requests renew a lease. Twitch lines come in through
/_gate/api/chat; hall lines go to Twitch as the viewer, with the viewer's token, kept in memory
for the session only.
"""

import hashlib
import hmac
import html
import json
import os
import asyncio
import base64
import re
import secrets
import socket
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
import urllib.error
import urllib.request
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SECRET = os.environ["GATE_SECRET"].encode()
API_TOKEN = os.environ["GATE_API_TOKEN"]
CODE_LEN = int(os.environ.get("GATE_CODE_LEN", "4"))
STATE_FILE = Path(os.environ.get("GATE_STATE", "/data/session.json"))
LEASE = int(os.environ.get("GATE_LEASE_SECONDS", "180"))

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
# The operator's channel and site address: compose passes them from the host's .env (kinozal.sh deploy)
TWITCH_CHANNEL = os.environ["TWITCH_CHANNEL"].lower()
PUBLIC_URL = os.environ["GATE_PUBLIC_URL"].rstrip("/")
OWNCAST_URL = os.environ.get("OWNCAST_URL", "http://owncast:8080").rstrip("/")
OWNCAST_ADMIN_PASSWORD = os.environ.get("OWNCAST_ADMIN_PASSWORD", "")
CHATTERS_FILE = STATE_FILE.with_name("chatters.json")
TWITCH_ENABLED = bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)
# marks a hall line sent to Twitch; must equal the bot's FILM_HALL_MARK
HALL_MARK = os.environ.get("HALL_MARK", "🎬 [KZ]")
HALL_POLL_SECONDS = float(os.environ.get("HALL_POLL_SECONDS", "2"))

P = "/_gate"
COOKIE = "kz"
COOKIE_TTL = 12 * 3600
HOST_COOKIE = "kzh"
HOST_TTL = 365 * 24 * 3600
FAIL_LIMIT, FAIL_WINDOW = 10, 600  # at most 10 wrong codes per 10 minutes per Twitch account, or per IP without Twitch
FAIL_IP_LIMIT = 50  # per IP with Twitch: viewers behind one carrier NAT share an address
OAUTH_COOKIE = "kzt"
PENDING_COOKIE = "kzp"  # passed Twitch, the code is still to be entered
PENDING_TTL = 1800
OAUTH_TTL = 600  # 10 minutes for the Twitch consent screen
# follows and subscriptions decide who gets in; write:chat sends hall lines to Twitch as the viewer
TWITCH_SCOPES = "user:read:follows user:read:subscriptions user:write:chat"
TWITCH_MSG_MAX = 500
HALL_SEEN_KEEP = 200
HTTP_TIMEOUT = 8
CHAT_BOT_NAME = "Twitch"  # lines from Twitch appear in Owncast chat under this name
CHAT_SCOPES = ["CAN_SEND_MESSAGES", "HAS_ADMIN_ACCESS"]  # chat history requires HAS_ADMIN_ACCESS
CHAT_BODY_MAX = 500
# Lines from Twitch appear in the hall chat as the author's twin, with its own nick and colour. A twin's
# nick cannot equal a viewer's nick (Owncast reserves bot names), hence the mark
TWIN_MARK = os.environ.get("TWIN_MARK", "[TW]")
TWINS_FILE = STATE_FILE.with_name("twins.json")
TWITCH_NAME = re.compile(r"^(\w{1,25}): (.+)$", re.S)
MENTION = re.compile(r"(?<![\w@])@(\w{1,25})")
RENAME_EVERY = 30  # a viewer's nick is reset at most once per 30 s

# Chat look for custom.js: Twitch nick colours and Twitch emotes. Owncast strips foreign images from
# messages, so the page draws emotes from a «name → id» map
LOOK_FILE = STATE_FILE.with_name("look.json")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
EMOTE_ID = re.compile(r"^[A-Za-z0-9_]{1,80}$")
EMOTE_NAME = re.compile(r"^\S{1,64}$")
EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/{scale}"
LOOK_COLORS_MAX = 5000
LOOK_EMOTES_MAX = 3000
TWITCH_EMOTES_TTL = 6 * 3600  # global and channel emotes and badges are refreshed every 6 hours
TWITCH_EMOTES_RETRY = 600
BADGE = re.compile(r"^[a-z0-9_-]{1,40}/[A-Za-z0-9_-]{1,40}$")  # Twitch badge «set/version»
BADGES_MAX = 6

# Hall chat bans: the streamer (an Owncast moderator) bans, the gate keeps the ban by Twitch account permanently
BANS_FILE = STATE_FILE.with_name("bans.json")
BAN_SYNC_SECONDS = 10

@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(hall_loop())
    yield
    task.cancel()


# Every handler is async: the session, the viewers' tokens and the state files change on the event
# loop only. A sync handler would run in a worker thread and race the loop (shared .tmp, session=None)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
failures: dict[str, list[float]] = {}


# --- state files ---

def read_json(path: Path, default):
    """A state file's JSON; default when it is missing or unreadable."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Replace a state file whole: a crash mid-write leaves the previous one."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


# --- session state ---

def load_session() -> dict | None:
    return read_json(STATE_FILE, None)


def save_session(s: dict | None) -> None:
    write_json(STATE_FILE, s)


session = load_session()
if session:
    session.setdefault("seen", time.time())


def expire() -> None:
    """Close the session if the bot has not checked in for longer than the lease."""
    global session
    if session and LEASE and time.time() - session["seen"] > LEASE:
        session = None
        save_session(None)
        forget_voices()


def touch() -> None:
    """Renew the session lease: the bot is alive."""
    if session:
        session["seen"] = time.time()
        save_session(session)


def sign(sid: str) -> str:
    return hmac.new(SECRET, sid.encode(), hashlib.sha256).hexdigest()


HOST_KEY = hmac.new(SECRET, b"host-dock", hashlib.sha256).hexdigest()[:32]


def same(a: str, b: str) -> bool:
    """Constant-time equality; compare_digest refuses non-ASCII str, which a client may send."""
    return hmac.compare_digest(a.encode(), b.encode())


def host_ok(req: Request) -> bool:
    return same(req.cookies.get(HOST_COOKIE, ""), HOST_KEY)


def who_sign(twitch_id: str, sid: str) -> str:
    return sign(f"who:{twitch_id}:{sid}")


def cookie_value(twitch_id: str | None) -> str:
    """Entry cookie: the session and, after a Twitch sign-in, the account, so a ban invalidates it."""
    sid = session["sid"]
    value = f"{sid}.{sign(sid)}"
    return f"{value}.{twitch_id}.{who_sign(twitch_id, sid)}" if twitch_id else value


def cookie_ok(req: Request) -> bool:
    expire()
    if not session:
        return False
    parts = req.cookies.get(COOKIE, "").split(".")
    if len(parts) not in (2, 4):
        return False
    sid, sig = parts[0], parts[1]
    if not (same(sid, session["sid"]) and same(sig, sign(sid))):
        return False
    if len(parts) == 4:
        twitch_id, wsig = parts[2], parts[3]
        if not same(wsig, who_sign(twitch_id, sid)) or banned(twitch_id):
            return False
    return True


# --- brute-force protection ---

def client_ip(req: Request) -> str:
    # Caddy without trusted_proxies overwrites X-Forwarded-For with the real client address
    xff = req.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() or (req.client.host if req.client else "?")


def throttled(key: str, limit: int = FAIL_LIMIT) -> bool:
    now = time.time()
    recent = [t for t in failures.get(key, []) if now - t < FAIL_WINDOW]
    if recent:
        failures[key] = recent
    else:
        failures.pop(key, None)
    return len(recent) >= limit


def failed(*keys: str) -> None:
    for key in keys:
        failures.setdefault(key, []).append(time.time())


def safe_next(n: str) -> str:
    """A path on this site, else "/". Control characters and spaces are refused: browsers and urlparse
    drop a tab or a newline, and «/<tab>/evil.com» would become the outside address «//evil.com»."""
    if (not n.startswith("/") or n[1:2] in ("/", "\\") or n.startswith(P)
            or any(c <= " " or c == "\x7f" for c in n)):
        return "/"
    return n


# --- check for Caddy forward_auth ---

@app.get(P + "/auth")
async def auth(req: Request):
    if host_ok(req) or cookie_ok(req):
        return Response(status_code=200)
    if req.headers.get("x-forwarded-method", "GET") == "GET":
        nxt = safe_next(req.headers.get("x-forwarded-uri", "/"))
        return RedirectResponse(f"{P}/enter?next={quote(nxt)}", status_code=302)
    return Response(status_code=401)


@app.get(P + "/live")
async def live(req: Request):
    """Session start, film title and gate time for the block under the player (customjavascript).

    Gate time keeps the timer correct when the viewer's clock is off. Signed-in viewers only.
    """
    if not (host_ok(req) or cookie_ok(req)):
        return Response(status_code=401)
    return JSONResponse({"started": session["started"] if session else None,
                         "title": session.get("title") if session else None,
                         "now": time.time(), "voice": viewer_voice(req)},
                        headers={"Cache-Control": "no-store"})


@app.get(P + "/now")
async def now_showing():
    """Session state, title and start for the KINO placeholder in OBS: no sign-in, any origin.

    The entry page shows the same to everyone; the answer carries no code and no stream links.
    """
    return JSONResponse({"open": bool(session), "title": session.get("title") if session else None,
                         "started": session["started"] if session else None, "now": time.time()},
                        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})


def viewer_voice(req: Request) -> bool | None:
    """Whether the viewer's hall lines go to Twitch; None when not signed in through Twitch (code, OBS dock).

    Viewer tokens live only in memory: after a gate restart there is no voice until the viewer passes
    Twitch again, and custom.js shows the "Reconnect" button meanwhile (no code needed).
    """
    if not TWITCH_ENABLED or not cookie_ok(req):
        return None
    parts = req.cookies.get(COOKIE, "").split(".")
    record = chatters.get(parts[2]) if len(parts) == 4 else None
    return None if not record else record.get("oc_id") in voices


# --- language: the entry page and the hall page in Russian or English ---

LANG_COOKIE = "kz_lang"  # the viewer's explicit choice; read by custom.js too, so not HttpOnly
LANGS = ("ru", "en")
LANG_TTL = 365 * 24 * 3600
# Browser languages read as Russian: Russian and the CIS languages whose speakers usually read it;
# custom.js keeps the same list (RU_READERS)
RU_READERS = ("ru", "uk", "be", "kk", "ky", "uz", "tg", "tk", "az", "hy")

TEXT = {
    "ru": {
        "title": "Кинозал",
        "switch_label": "Switch to English",
        "closed_lead": "Сейчас сеанса нет",
        "closed_hint": "Когда начнётся кино, ссылка и код появятся в чате Twitch.",
        "now": "Сегодняшний фильм:",
        "enter": "Войти",
        "missed": "Пропустил код – напиши !film в чате",
        "code_after_twitch": "Теперь код из чата Twitch",
        "followers_only": "Вход только для фолловеров и сабов канала {chan}",
        "twitch": "Войти через Twitch",
        "then_code": "После введи код из чата Twitch для просмотра фильма",
        "code": "Введи код из чата Twitch",
        "need_twitch": "Сначала войди через Twitch.",
        "too_many": "Слишком много попыток. Подожди пару минут.",
        "bad_code": "Неверный код.",
        "twitch_cancelled": "Вход через Twitch отменён. Без него в кинозал не войти – попробуй ещё раз.",
        "twitch_failed": "Вход через Twitch не удался, попробуй ещё раз.",
        "banned": "Вход в кинозал для тебя закрыт.",
        "not_follower": "Вход через Twitch – для фолловеров и сабов канала {chan}. Зафоловь канал и попробуй снова.",
        "unavailable": "Twitch или чат кинозала не отвечает. Попробуй ещё раз через минуту.",
        "to_hall": "В кинозал",
        "to_entry": "Ко входу",
    },
    "en": {
        "title": "Cinema",
        "switch_label": "Переключить на русский",
        "closed_lead": "There is no session right now",
        "closed_hint": "When the film starts, the link and the code appear in the Twitch chat.",
        "now": "Tonight's film:",
        "enter": "Enter",
        "missed": "Missed the code? Type !film in the chat",
        "code_after_twitch": "Now the code from the Twitch chat",
        "followers_only": "Only for followers and subscribers of {chan}",
        "twitch": "Sign in with Twitch",
        "then_code": "Then enter the code from the Twitch chat to watch the film",
        "code": "Enter the code from the Twitch chat",
        "need_twitch": "Sign in with Twitch first.",
        "too_many": "Too many attempts. Wait a couple of minutes.",
        "bad_code": "Wrong code.",
        "twitch_cancelled": "Twitch sign-in was cancelled. The cinema opens only after it – try again.",
        "twitch_failed": "Twitch sign-in failed, try again.",
        "banned": "The cinema is closed to you.",
        "not_follower": "Twitch sign-in is for followers and subscribers of {chan}. Follow the channel and try again.",
        "unavailable": "Twitch or the cinema chat is not answering. Try again in a minute.",
        "to_hall": "To the cinema",
        "to_entry": "To the entry page",
    },
}


# The link preview (Discord, Telegram): a crawler has no cookie and lands here, so the page carries
# the same bilingual card that kinozal.sh setup gives Owncast
PREVIEW_TITLE = f'{TEXT["ru"]["title"]} · {TEXT["en"]["title"]}'
PREVIEW_SUMMARY = f"Кино со стрима {TWITCH_CHANNEL} · Films from the {TWITCH_CHANNEL} stream"


def lang_of(req: Request) -> str:
    """The viewer's choice (kz_lang), else the browser's first language."""
    lang = req.cookies.get(LANG_COOKIE, "")
    return lang if lang in LANGS else browser_lang(req.headers.get("accept-language", ""))


def browser_lang(header: str) -> str:
    """ru when the browser's preferred language reads Russian, en otherwise; no header – ru, the channel's."""
    best, best_q = "", -1.0
    for part in header.split(","):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if tag and q > best_q:
            best, best_q = tag.split("-")[0].strip().lower(), q
    if not best or best == "*":
        return LANGS[0]
    return "ru" if best in RU_READERS else "en"


def tr(lang: str, key: str, **values: str) -> str:
    text = TEXT[lang][key]
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def with_lang(nxt: str, lang: str) -> str:
    """The site's address in the viewer's language: Owncast takes its own only from ?lang=, else from the browser."""
    u = urlparse(nxt)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "lang"]
    if lang == "en":
        query.append(("lang", "en"))
    return safe_next(urlunparse(u._replace(query=urlencode(query))))


@app.get(P + "/lang")
async def set_lang(to: str = "", next: str = "/"):
    """The entry page's language switch: remembers the choice for the gate and custom.js, then the entry page again."""
    resp = RedirectResponse(f"{P}/enter?next={quote(safe_next(next))}", status_code=303)
    resp.set_cookie(LANG_COOKIE, to if to in LANGS else LANGS[0], max_age=LANG_TTL,
                    secure=True, samesite="lax", path="/")
    return resp


# --- entry page ---

@app.get(P + "/enter")
async def enter_form(req: Request, next: str = "/"):
    nxt, lang = safe_next(next), lang_of(req)
    expire()
    if not session:
        return page(lang, closed=True)
    if cookie_ok(req):
        return RedirectResponse(with_lang(nxt, lang), status_code=303)
    who = pending_user(req)
    return page(lang, nxt=nxt, who=who["name"] if who else None)


@app.post(P + "/enter")
async def enter_submit(req: Request):
    form = parse_qs((await req.body()).decode(errors="replace"))
    code = form.get("code", [""])[0].strip()
    nxt, lang = safe_next(form.get("next", ["/"])[0]), lang_of(req)
    expire()
    if not session:
        return page(lang, closed=True)

    who = pending_user(req)
    name = who["name"] if who else None
    if TWITCH_ENABLED and not who:
        return page(lang, nxt=nxt, error=tr(lang, "need_twitch"), status=401)
    # Counted per Twitch account, so one viewer's typos do not lock out others behind the same address
    ip, account = client_ip(req), f"tw:{pending_id(req)}" if who else None
    if throttled(ip, FAIL_IP_LIMIT if account else FAIL_LIMIT) or (account and throttled(account)):
        return page(lang, nxt=nxt, who=name, error=tr(lang, "too_many"), status=429)
    if not same(code, session["code"]):
        failed(ip, *([account] if account else []))
        return page(lang, nxt=nxt, who=name, error=tr(lang, "bad_code"), status=401)

    # Passed Twitch: chat under the Twitch name; Twitch not configured: anonymous
    site = with_lang(nxt, lang)
    resp = chat_handoff(who["oc_token"], site, lang) if who else RedirectResponse(site, status_code=303)
    resp.set_cookie(COOKIE, cookie_value(pending_id(req) if who else None), max_age=COOKIE_TTL,
                    httponly=True, secure=True, samesite="lax", path="/")
    resp.delete_cookie(PENDING_COOKIE, path=P)
    return resp


def pending_sign(twitch_id: str) -> str:
    return sign(f"pending:{twitch_id}:{session['sid']}")


def pending_id(req: Request) -> str | None:
    """Twitch id of a viewer who passed Twitch in this session and has not entered the code yet."""
    if not session:
        return None
    twitch_id, _, sig = req.cookies.get(PENDING_COOKIE, "").partition(".")
    if not twitch_id or not same(sig, pending_sign(twitch_id)):
        return None
    return twitch_id


def pending_user(req: Request) -> dict | None:
    """That viewer's record in chatters."""
    twitch_id = pending_id(req)
    return chatters.get(twitch_id) if twitch_id else None


# --- permanent streamer entry (OBS chat dock) ---

@app.get(P + "/host")
async def host_login(req: Request, key: str = "", next: str = "/embed/chat/readwrite/"):
    ip = client_ip(req)
    if throttled(ip):
        return Response(status_code=429)
    if not same(key, HOST_KEY):
        failed(ip)
        return Response(status_code=404)
    # after a Twitch sign-in by the streamer the dock writes under their nick with moderator rights
    own, lang = streamer_chatter(), lang_of(req)
    site = with_lang(safe_next(next), lang)
    resp = chat_handoff(own["oc_token"], site, lang) if own else RedirectResponse(site, status_code=303)
    resp.set_cookie(HOST_COOKIE, HOST_KEY, max_age=HOST_TTL,
                    httponly=True, secure=True, samesite="lax", path="/")
    return resp


# --- Twitch sign-in ---

class Upstream(Exception):
    """Twitch or Owncast did not respond or responded unexpectedly."""


class TwinDisabled(Upstream):
    """A Twitch viewer's twin is disabled (banned) in Owncast chat: a fresh token is rejected too."""


class TwinUnverified(Upstream):
    """Owncast refused the twin's token and the ban check failed: the line may come from a banned viewer."""


def _http(method: str, url: str, *, data: dict | None = None, json_body: dict | None = None,
          headers: dict | None = None) -> tuple[int, dict | list | None]:
    """One request: (status, JSON or None). Network errors and timeouts raise Upstream."""
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
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
    # HTTPException: a peer restarting mid-answer (IncompleteRead, RemoteDisconnected)
    except (urllib.error.URLError, HTTPException, TimeoutError, OSError) as e:
        raise Upstream(f"{method} {url.split('?')[0]}: {e}") from e
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


async def http(*args, **kwargs):
    return await asyncio.to_thread(_http, *args, **kwargs)


def load_chatters() -> dict:
    return read_json(CHATTERS_FILE, {})


def save_chatters(c: dict) -> None:
    write_json(CHATTERS_FILE, c)


chatters = load_chatters()  # twitch id → {"login", "name", "oc_id", "oc_token"}
channel_id: str | None = None


def oauth_cookie(state_: str, nxt: str) -> str:
    raw = f"{state_}|{nxt}"
    return base64.urlsafe_b64encode(raw.encode()).decode() + "." + sign(raw)


def read_oauth_cookie(value: str) -> tuple[str, str] | None:
    b64, _, sig = value.partition(".")
    try:
        raw = base64.urlsafe_b64decode(b64.encode()).decode()
    except ValueError:
        return None
    if not same(sig, sign(raw)):
        return None
    state_, _, nxt = raw.partition("|")
    return state_, nxt


async def twitch_user(token: str) -> tuple[dict, str]:
    """The viewer by their token, and the channel id (looked up by login once)."""
    global channel_id
    hdrs = {"Authorization": f"Bearer {token}", "Client-Id": TWITCH_CLIENT_ID}
    status, me = await http("GET", "https://api.twitch.tv/helix/users", headers=hdrs)
    if status != 200 or not me or not me.get("data"):
        raise Upstream(f"helix/users: HTTP {status}")
    if channel_id is None:
        status, ch = await http("GET", f"https://api.twitch.tv/helix/users?login={quote(TWITCH_CHANNEL)}",
                                headers=hdrs)
        if status != 200 or not ch or not ch.get("data"):
            raise Upstream(f"helix/users?login: HTTP {status}")
        channel_id = ch["data"][0]["id"]
    return me["data"][0], channel_id


async def may_watch(token: str, user_id: str, chan: str) -> bool:
    """The streamer, a follower or a subscriber of the channel, checked with the viewer's own token."""
    if user_id == chan:
        return True
    hdrs = {"Authorization": f"Bearer {token}", "Client-Id": TWITCH_CLIENT_ID}
    status, follows = await http(
        "GET", f"https://api.twitch.tv/helix/channels/followed?user_id={user_id}&broadcaster_id={chan}",
        headers=hdrs)
    if status != 200:
        raise Upstream(f"helix/channels/followed: HTTP {status}")
    if follows and follows.get("data"):
        return True
    status, _ = await http(
        "GET", f"https://api.twitch.tv/helix/subscriptions/user?broadcaster_id={chan}&user_id={user_id}",
        headers=hdrs)
    if status == 200:
        return True
    if status == 404:
        return False
    raise Upstream(f"helix/subscriptions/user: HTTP {status}")


async def revoke(token: str) -> None:
    """Revoke a viewer token that is no longer needed; a failure does not block sign-in."""
    with suppress(Upstream):
        await http("POST", "https://id.twitch.tv/oauth2/revoke",
                   data={"client_id": TWITCH_CLIENT_ID, "token": token})


def owncast_admin() -> dict:
    raw = f"admin:{OWNCAST_ADMIN_PASSWORD}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def chat_identity(user: dict) -> dict:
    """Owncast chat user for a Twitch viewer, always with the Twitch name.

    Within a session it is the same user (second device, repeated sign-in); a new session gets a
    new one, so the hall name always matches Twitch. Owncast rejects the previous token once the
    user is disabled in chat (ban) or the Owncast database is lost. A disabled user keeps their
    token: watching works, writing does not; a new user would undo the ban.
    """
    known = chatters.get(user["id"])
    if known:
        status, _ = await http("GET", f"{OWNCAST_URL}/api/chat?accessToken={quote(known['oc_token'])}")
        if status == 200 and session and known.get("sid") == session["sid"]:
            return known
        if status not in (200, 401):
            raise Upstream(f"owncast /api/chat: HTTP {status}")
        if status == 401:
            status, disabled = await http("GET", f"{OWNCAST_URL}/api/admin/chat/users/disabled",
                                          headers=owncast_admin())
            if status != 200 or not isinstance(disabled, list):
                raise Upstream(f"owncast disabled users: HTTP {status}")
            if any(d.get("id") == known["oc_id"] for d in disabled):
                return known
    name = user.get("display_name") or user["login"]
    if name.casefold() != user["login"].casefold():
        name = user["login"]  # display name in another script: the chat shows the login, as Twitch spells it
    status, reg = await http("POST", f"{OWNCAST_URL}/api/chat/register", json_body={"displayName": name})
    if status != 200 or not reg or not reg.get("accessToken"):
        raise Upstream(f"owncast /api/chat/register: HTTP {status}")
    if user["login"].casefold() == TWITCH_CHANNEL:
        await make_moderator(reg["id"])  # only the streamer deletes messages and bans
    record = {"login": user["login"], "name": name, "oc_id": reg["id"], "oc_token": reg["accessToken"],
              "sid": session["sid"] if session else None}
    chatters[user["id"]] = record
    save_chatters(chatters)
    return record


# --- chat look: Twitch nick colours and emotes ---

def load_look() -> dict:
    data = read_json(LOOK_FILE, {})
    try:
        return {"colors": dict(data.get("colors") or {}), "emotes": dict(data.get("emotes") or {}),
                "badges": dict(data.get("badges") or {})}
    except (ValueError, AttributeError):
        return {"colors": {}, "emotes": {}, "badges": {}}


# colors: nick (casefold) → #RRGGBB; emotes: name → id of an emote seen in Twitch;
# badges: nick (casefold) → ["moderator/1", "subscriber/12", ...] – Twitch chat badges
look = load_look()


def save_look() -> None:
    write_json(LOOK_FILE, look)


def _cap(d: dict, limit: int) -> None:
    while len(d) > limit:
        d.pop(next(iter(d)))


def remember_look(name: str | None, color, emotes, badges=None) -> None:
    """Nick colour, emotes and badges of a Twitch line (sent by the bot); malformed values are skipped silently."""
    changed = False
    if name and isinstance(badges, list):
        key = name.casefold()
        value = [b for b in badges if isinstance(b, str) and BADGE.match(b)][:BADGES_MAX]
        if look["badges"].get(key, []) != value:
            look["badges"].pop(key, None)
            if value:
                look["badges"][key] = value
            changed = True
    if name and isinstance(color, str) and HEX_COLOR.match(color):
        key, value = name.casefold(), color.upper()
        if look["colors"].get(key) != value:
            look["colors"].pop(key, None)
            look["colors"][key] = value
            changed = True
    if isinstance(emotes, dict):
        for em_name, em_id in list(emotes.items())[:50]:
            if (isinstance(em_name, str) and isinstance(em_id, str) and EMOTE_NAME.match(em_name)
                    and EMOTE_ID.match(em_id) and look["emotes"].get(em_name) != em_id):
                look["emotes"].pop(em_name, None)
                look["emotes"][em_name] = em_id
                changed = True
    if changed:
        _cap(look["colors"], LOOK_COLORS_MAX)
        _cap(look["emotes"], LOOK_EMOTES_MAX)
        _cap(look["badges"], LOOK_COLORS_MAX)
        save_look()


app_token: dict = {"token": None, "until": 0.0}
twitch_emotes: dict = {"at": 0.0, "map": {}, "badges": {}}


async def app_access_token() -> str:
    """Twitch app token (client credentials) for Helix calls without a viewer."""
    if app_token["token"] and time.time() < app_token["until"]:
        return app_token["token"]
    status, tok = await http("POST", "https://id.twitch.tv/oauth2/token", data={
        "client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"})
    if status != 200 or not tok or not tok.get("access_token"):
        raise Upstream(f"oauth2/token (app): HTTP {status}")
    app_token.update(token=tok["access_token"], until=time.time() + int(tok.get("expires_in") or 3600) - 300)
    return app_token["token"]


async def channel_emotes() -> dict:
    """Twitch global and channel emotes (name → id), refreshed every TWITCH_EMOTES_TTL.

    Also badge images, global and channel (subscriber) ones, in twitch_emotes["badges"]:
    «set/version» → [1x image, 2x image, title].
    """
    global channel_id
    now = time.time()
    if not TWITCH_ENABLED or now - twitch_emotes["at"] < TWITCH_EMOTES_TTL:
        return twitch_emotes["map"]
    twitch_emotes["at"] = now - TWITCH_EMOTES_TTL + TWITCH_EMOTES_RETRY  # on failure, retry in 10 minutes
    try:
        hdrs = {"Authorization": f"Bearer {await app_access_token()}", "Client-Id": TWITCH_CLIENT_ID}
        if channel_id is None:
            status, ch = await http("GET", f"https://api.twitch.tv/helix/users?login={quote(TWITCH_CHANNEL)}",
                                    headers=hdrs)
            if status != 200 or not ch or not ch.get("data"):
                raise Upstream(f"helix/users?login: HTTP {status}")
            channel_id = ch["data"][0]["id"]
        found = {}
        for url in ("https://api.twitch.tv/helix/chat/emotes/global",
                    f"https://api.twitch.tv/helix/chat/emotes?broadcaster_id={channel_id}"):
            status, data = await http("GET", url, headers=hdrs)
            if status != 200 or not data or not isinstance(data.get("data"), list):
                raise Upstream(f"{url.split('?')[0].split('/helix/')[1]}: HTTP {status}")
            found.update({e["name"]: e["id"] for e in data["data"]
                          if EMOTE_NAME.match(str(e.get("name", ""))) and EMOTE_ID.match(str(e.get("id", "")))})
        badges = {}
        for url in ("https://api.twitch.tv/helix/chat/badges/global",  # channel second: its badges override global ones
                    f"https://api.twitch.tv/helix/chat/badges?broadcaster_id={channel_id}"):
            status, data = await http("GET", url, headers=hdrs)
            if status != 200 or not data or not isinstance(data.get("data"), list):
                raise Upstream(f"{url.split('?')[0].split('/helix/')[1]}: HTTP {status}")
            for badge_set in data["data"]:
                for v in badge_set.get("versions") or []:
                    key = f'{badge_set.get("set_id")}/{v.get("id")}'
                    if BADGE.match(key) and str(v.get("image_url_1x", "")).startswith("https://"):
                        badges[key] = [v["image_url_1x"], v.get("image_url_2x") or v["image_url_1x"],
                                       str(v.get("title") or "")[:60]]
    except Upstream as e:
        print(f"gate: смайлы и значки Twitch: {e}", flush=True)
        return twitch_emotes["map"]
    twitch_emotes.update(at=now, map=found, badges=badges)
    return found


async def viewer_color(token: str, user_id: str) -> str | None:
    """The viewer's Twitch chat nick colour (with their token); None if unset or on error."""
    hdrs = {"Authorization": f"Bearer {token}", "Client-Id": TWITCH_CLIENT_ID}
    try:
        status, data = await http("GET", f"https://api.twitch.tv/helix/chat/color?user_id={quote(user_id)}",
                                  headers=hdrs)
    except Upstream:
        return None
    if status != 200 or not data or not data.get("data"):
        return None
    color = data["data"][0].get("color") or ""
    return color if HEX_COLOR.match(color) else None


@app.get(P + "/look")
async def chat_look(req: Request):
    """For custom.js: Twitch nick colours and badges, emotes (global, channel and seen in Twitch chat)."""
    if not (host_ok(req) or cookie_ok(req)):
        return Response(status_code=401)
    emotes = {**await channel_emotes(), **look["emotes"]}
    used = {b for badges in look["badges"].values() for b in badges}
    images = {b: twitch_emotes["badges"][b] for b in used if b in twitch_emotes["badges"]}
    return JSONResponse({"colors": look["colors"], "emotes": emotes, "url": EMOTE_URL,
                         "badges": look["badges"], "badgeImages": images},
                        headers={"Cache-Control": "private, max-age=20"})


# --- who is in chat: the participant list and the «@» hint in custom.js ---

USERS_TTL = 5  # open "Chat members" panels query Owncast at most once per 5 s in total
# A failure is cached too: with Owncast hung, every request would wait for the timeout in the shared thread
# pool (also used by the chat bridge and ban sync). One Owncast request at a time; the rest wait for its answer
users_cache: dict = {"at": 0.0, "users": None}
users_lock = asyncio.Lock()


def chat_users(clients) -> list[dict]:
    """Users connected to Owncast chat: one entry per user (a user may have several tabs), bots excluded."""
    seen: dict[str, dict] = {}
    for c in clients if isinstance(clients, list) else []:
        u = c.get("user") if isinstance(c, dict) else None
        if not isinstance(u, dict) or u.get("isBot") or not isinstance(u.get("displayName"), str):
            continue
        mod = "MODERATOR" in (u.get("scopes") or []) or bool(u.get("isModerator"))
        key = u.get("id") or u["displayName"]
        if key in seen:
            seen[key]["mod"] = seen[key]["mod"] or mod
        else:
            seen[key] = {"name": u["displayName"], "mod": mod}
    return sorted(seen.values(), key=lambda x: x["name"].casefold())


@app.get(P + "/users")
async def users(req: Request):
    """For custom.js: who is in the hall chat now. Signed-in viewers only; 502 if Owncast does not respond."""
    if not (host_ok(req) or cookie_ok(req)):
        return Response(status_code=401)
    async with users_lock:
        if not users_cache["at"] or time.monotonic() - users_cache["at"] > USERS_TTL:
            try:
                status, clients = await http("GET", f"{OWNCAST_URL}/api/admin/chat/clients", headers=owncast_admin())
            except Upstream:
                status, clients = 0, None
            users_cache.update(at=time.monotonic(), users=chat_users(clients) if status == 200 else None)
    if users_cache["users"] is None:
        return Response(status_code=502)
    return JSONResponse({"users": users_cache["users"]}, headers={"Cache-Control": "private, max-age=5"})


# --- moderation: the streamer moderates the chat, their bans are permanent ---

def load_bans() -> dict:
    data = read_json(BANS_FILE, {})
    return data if isinstance(data, dict) else {}


bans: dict = load_bans()  # "id:<Twitch id>" | "tw:<nick>" → {"login", "name", "oc_ids": [...], "at"}
# Ban sync and unban are serialized: a sync started before an unban would restore the lifted ban
bans_lock = asyncio.Lock()


def save_bans() -> None:
    write_json(BANS_FILE, bans)


def banned(twitch_id: str | None = None, login: str | None = None) -> bool:
    if twitch_id and f"id:{twitch_id}" in bans:
        return True
    key = login.casefold() if login else None
    return bool(key) and any(b.get("login") == key for b in bans.values())


async def sync_bans() -> None:
    """Sync with users disabled in Owncast chat under the bans lock (called by the loop and the "Bans" window)."""
    async with bans_lock:
        await _sync_bans()


async def _sync_bans() -> None:
    """Record a new ban by Twitch account, forget a lifted one.

    A hall viewer is matched by their chat user (chatters), a Twitch viewer by the twin «nick [TW]».
    A ban is lifted in the "Bans" window of the hall chat (`POST /_gate/unban`) or in the Owncast admin;
    a twin's ban is lifted by the admin only while the twin is still in the chat history.
    """
    status, disabled = await http("GET", f"{OWNCAST_URL}/api/admin/chat/users/disabled", headers=owncast_admin())
    if status != 200 or not isinstance(disabled, list):
        raise Upstream(f"owncast disabled users: HTTP {status}")
    twins_off, twins_seen = await twins_in_history() if TWIN_MARK else ({}, set())
    off = {d.get("id") for d in disabled} | set(twins_off)
    disabled = list(disabled) + [{"id": i, "displayName": f"{login} {TWIN_MARK}"} for i, login in twins_off.items()]
    by_oc = {r.get("oc_id"): (tid, r) for tid, r in chatters.items()}
    changed = False
    for d in disabled:
        oc_id, shown = d.get("id"), str(d.get("displayName") or "")
        if oc_id in by_oc:
            tid, record = by_oc[oc_id]
            key, login = f"id:{tid}", str(record.get("login", "")).casefold()
        elif TWIN_MARK and shown.endswith(f" {TWIN_MARK}"):
            login = shown[:-len(TWIN_MARK) - 1].strip().casefold()
            key = f"tw:{login}"
        else:
            continue
        entry = bans.get(key)
        if entry is None:
            # name is the Owncast chat nick at ban time: it labels the IP ban («Banning of <nick>») that unban removes
            bans[key] = {"login": login, "name": shown, "oc_ids": [oc_id], "at": int(time.time())}
            print(f"gate: бан в кинозале: {login}", flush=True)
            changed = True
        elif oc_id not in entry["oc_ids"]:
            entry["oc_ids"].append(oc_id)
            changed = True
    for key, entry in list(bans.items()):
        ids = entry.get("oc_ids", [])
        if any(i in off for i in ids):
            continue
        # A twin is visible only through the history: one gone from it keeps its ban until an unban
        if key.startswith("tw:") and not any(i in twins_seen for i in ids):
            continue
        del bans[key]
        print(f"gate: бан снят: {entry.get('login')}", flush=True)
        changed = True
    if changed:
        save_bans()


async def make_moderator(oc_id: str) -> None:
    """Make an Owncast chat user a moderator, able to delete messages and ban (the streamer only)."""
    status, _ = await http("POST", f"{OWNCAST_URL}/api/admin/chat/users/setmoderator", headers=owncast_admin(),
                           json_body={"userId": oc_id, "isModerator": True})
    if status != 200:
        raise Upstream(f"owncast setmoderator: HTTP {status}")


def streamer_chatter() -> dict | None:
    """The streamer's chat user, if they signed in through Twitch; the dock's permanent entry uses it too."""
    return next((r for r in chatters.values() if str(r.get("login", "")).casefold() == TWITCH_CHANNEL), None)


def streamer_ok(req: Request) -> bool:
    """Whether the request is the streamer's: the dock's permanent pass or a Twitch sign-in as the channel."""
    if host_ok(req):
        return True
    if not cookie_ok(req):
        return False
    parts = req.cookies.get(COOKIE, "").split(".")
    record = chatters.get(parts[2]) if len(parts) == 4 else None  # the id is signed and cookie_ok has verified it
    return bool(record) and str(record.get("login", "")).casefold() == TWITCH_CHANNEL


@app.get(P + "/bans")
async def bans_list(req: Request):
    """For the "Bans" window in custom.js (streamer only): who is banned, newest first.

    Syncs with Owncast before answering, so a ban made a second ago is listed. If Owncast does not
    respond, the list comes from the file.
    """
    if not streamer_ok(req):
        return Response(status_code=403)
    try:
        await sync_bans()
    except Upstream as e:
        print(f"gate: сверка банов: {e}", flush=True)
    items = [{"key": k, "name": str(v.get("name") or v.get("login") or "").removesuffix(f" {TWIN_MARK}"),
              "twitch": k.startswith("tw:"), "at": v.get("at") or 0} for k, v in bans.items()]
    items.sort(key=lambda x: -x["at"])
    return JSONResponse({"bans": items}, headers={"Cache-Control": "no-store"})


@app.post(P + "/unban")
async def unban(req: Request):
    """{"key": key from /_gate/bans} (streamer only): enable the chat users, lift the IP ban, forget the ban.

    Banning a connected viewer, Owncast also bans the IPs of their connections with the note
    «Banning of <nick>»; enabling the user does not lift it, and the viewer would stay out of chat.
    """
    if not streamer_ok(req):
        return Response(status_code=403)
    try:
        key = json.loads(await req.body() or b"{}").get("key")
    except (ValueError, AttributeError):
        key = None
    async with bans_lock:
        entry = bans.get(key) if isinstance(key, str) else None
        if entry is None:
            return Response(status_code=404)
        try:
            for oc_id in entry.get("oc_ids", []):
                status, _ = await http("POST", f"{OWNCAST_URL}/api/admin/chat/users/setenabled",
                                       headers=owncast_admin(), json_body={"userId": oc_id, "enabled": True})
                if status != 200:
                    raise Upstream(f"owncast setenabled: HTTP {status}")
            names = {f"banning of {n}".casefold() for n in (entry.get("name"), entry.get("login")) if n}
            status, ips = await http("GET", f"{OWNCAST_URL}/api/admin/chat/users/ipbans", headers=owncast_admin())
            if status != 200 or not isinstance(ips, list):
                raise Upstream(f"owncast ipbans: HTTP {status}")
            for ban in ips:
                if isinstance(ban, dict) and str(ban.get("notes") or "").casefold() in names and ban.get("ipAddress"):
                    status, _ = await http("POST", f"{OWNCAST_URL}/api/admin/chat/users/ipbans/remove",
                                           headers=owncast_admin(), json_body={"value": ban["ipAddress"]})
                    if status != 200:
                        raise Upstream(f"owncast ipbans/remove: HTTP {status}")
        except Upstream as e:
            print(f"gate: разбан {entry.get('login')}: {e}", flush=True)
            return Response(status_code=502)
        del bans[key]
        save_bans()
    print(f"gate: бан снят из кинозала: {entry.get('login')}", flush=True)
    return Response(status_code=204)


# --- chat bridge: Owncast ↔ bot ---

chat_token: str | None = None


async def owncast_chat_token(fresh: bool = False) -> str:
    """The «Twitch» integration token: found in Owncast or created, then cached."""
    global chat_token
    if chat_token and not fresh:
        return chat_token
    status, tokens = await http("GET", f"{OWNCAST_URL}/api/admin/accesstokens", headers=owncast_admin())
    if status != 200 or not isinstance(tokens, list):
        raise Upstream(f"owncast accesstokens: HTTP {status}")
    for t in tokens:
        if t.get("displayName") == CHAT_BOT_NAME and set(CHAT_SCOPES) <= set(t.get("scopes") or []):
            chat_token = t["accessToken"]
            return chat_token
    status, t = await http("POST", f"{OWNCAST_URL}/api/admin/accesstokens/create", headers=owncast_admin(),
                           json_body={"name": CHAT_BOT_NAME, "scopes": CHAT_SCOPES})
    if status != 200 or not t or not t.get("accessToken"):
        raise Upstream(f"owncast accesstokens/create: HTTP {status}")
    chat_token = t["accessToken"]
    return chat_token


async def integration(method: str, path: str, json_body: dict | None = None):
    """Request to the Owncast integrations API; a revoked token (401) is fetched again once."""
    for fresh in (False, True):
        token = await owncast_chat_token(fresh)
        status, data = await http(method, f"{OWNCAST_URL}{path}", json_body=json_body,
                                  headers={"Authorization": f"Bearer {token}"})
        if status != 401:
            break
    if status != 200:
        raise Upstream(f"owncast {path}: HTTP {status}")
    return data


class _Text(HTMLParser):
    """Owncast message text without markup; an emoji image becomes its alt."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f" {alt} ")
        elif tag in ("br", "p"):
            self.parts.append(" ")


# Reply mark from custom.js: the source line id as zero-width characters between two U+2063. Owncast
# draws a reply plate from it; Twitch needs neither the mark nor a fragment of it left by Backspace.
# Matched only next to U+2063: U+200D alone joins compound emoji
REPLY_MARK = re.compile("\u2063[\u200b\u200c\u200d\u2060]*\u2063?|[\u200b\u200c\u200d\u2060]*\u2063")


def plain(body: str) -> str:
    parser = _Text()
    parser.feed(body)
    parser.close()
    text = " ".join(REPLY_MARK.sub("", "".join(parser.parts)).split())
    # Owncast puts a line break after the «@» of a mention from the hint; Twitch does not highlight «@ nick»
    return re.sub(r"@\s+(?=\w)", "@", text)


# A line starts with «nick:», so block markup (lists, headings, quotes) cannot take effect;
# inline markup is escaped. The dot is left alone, or links break
_INLINE_MD = re.compile(r"([\\`*_\[\]<>~])")


def escape_markdown(text: str) -> str:
    return _INLINE_MD.sub(r"\\\1", text)


def hall_markdown(text: str) -> str:
    """A Twitch line for Owncast chat: markup escaped, @nick mentions in bold."""
    out, at = [], 0
    for m in MENTION.finditer(text):
        out += [escape_markdown(text[at:m.start()]), f"**@{escape_markdown(m.group(1))}**"]
        at = m.end()
    out.append(escape_markdown(text[at:]))
    return "".join(out)


# --- Twitch viewers' twins in the hall chat ---

def load_twins() -> dict:
    return read_json(TWINS_FILE, {})


twins: dict[str, str] = load_twins()  # Twitch nick (casefold) → Owncast integration token


async def twin_token(name: str, fresh: bool = False) -> str:
    """Token of the twin «nick [TW]»: from the file, from Owncast or new (send-only scope)."""
    key = name.casefold()
    if key in twins and not fresh:
        return twins[key]
    twin = f"{name} {TWIN_MARK}".strip()
    status, tokens = await http("GET", f"{OWNCAST_URL}/api/admin/accesstokens", headers=owncast_admin())
    if status != 200 or not isinstance(tokens, list):
        raise Upstream(f"owncast accesstokens: HTTP {status}")
    found = next((t["accessToken"] for t in tokens
                  if t.get("displayName") == twin and "CAN_SEND_MESSAGES" in (t.get("scopes") or [])), None)
    if not found:
        status, t = await http("POST", f"{OWNCAST_URL}/api/admin/accesstokens/create", headers=owncast_admin(),
                               json_body={"name": twin, "scopes": ["CAN_SEND_MESSAGES"]})
        if status != 200 or not t or not t.get("accessToken"):
            raise Upstream(f"owncast accesstokens/create: HTTP {status}")
        found = t["accessToken"]
    twins[key] = found
    write_json(TWINS_FILE, twins)
    return found


async def twins_in_history() -> tuple[dict[str, str], set[str]]:
    """Twins in the admin chat history: the banned ones (user id → nick, casefold) and every one seen.

    Owncast lists disabled integrations neither among disabled users nor among tokens, only in the
    admin chat history (user.disabledAt); a twin whose lines left the history is not seen at all.
    """
    status, history = await http("GET", f"{OWNCAST_URL}/api/admin/chat/messages", headers=owncast_admin())
    if status != 200 or not isinstance(history, list):
        raise Upstream(f"owncast chat history: HTTP {status}")
    off, seen, tail = {}, set(), f" {TWIN_MARK}"
    for m in history:
        u = m.get("user") or {}
        shown = str(u.get("displayName") or "")
        if TWIN_MARK and u.get("id") and shown.endswith(tail):
            seen.add(u["id"])
            if u.get("disabledAt"):
                off[u["id"]] = shown[:-len(tail)].strip().casefold()
    return off, seen


async def disabled_twins() -> dict[str, str]:
    """Banned twins: user id → nick (casefold)."""
    return (await twins_in_history())[0]


async def twin_disabled(name: str) -> bool:
    """Whether the twin «nick [TW]» is disabled (banned) in Owncast chat."""
    return name.casefold() in (await disabled_twins()).values()


async def say_as_twin(name: str, text: str) -> None:
    """Post a Twitch viewer's line to Owncast chat as their twin; a revoked token is fetched again.

    401 means the token was revoked or the twin was banned. A banned twin gets no new token: that would
    be a new user with the same nick, and the ban would not hold.
    """
    for fresh in (False, True):
        token = await twin_token(name, fresh)
        status, _ = await http("POST", f"{OWNCAST_URL}/api/integrations/chat/send",
                               json_body={"body": hall_markdown(text)}, headers={"Authorization": f"Bearer {token}"})
        if status != 401:
            break
        try:
            disabled = not fresh and await twin_disabled(name)
        except Upstream as e:
            raise TwinUnverified(f"owncast chat/send ({name}): HTTP 401, ban check failed: {e}") from e
        if disabled:
            raise TwinDisabled(f"owncast chat/send ({name}): the twin is disabled in chat")
    if status == 401:
        raise TwinDisabled(f"owncast chat/send ({name}): the twin is disabled in chat")
    if status != 200:
        raise Upstream(f"owncast chat/send ({name}): HTTP {status}")


# --- hall nick = Twitch nick ---

renamed: dict[str, float] = {}  # Owncast user id → when the nick was last reset


def _ws_name_change(oc_token: str, name: str) -> None:
    """Rename an Owncast user with their own token: a websocket carrying one NAME_CHANGE message."""
    url = urlparse(OWNCAST_URL)
    host, port = url.hostname, url.port or 80
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    with socket.create_connection((host, port), timeout=HTTP_TIMEOUT) as sock:
        sock.sendall((f"GET /ws?accessToken={quote(oc_token)} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                      f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        if not sock.recv(4096).startswith(b"HTTP/1.1 101"):
            raise Upstream("owncast /ws: no upgrade")
        time.sleep(1.5)  # Owncast rejects messages right after connecting (flood protection)
        payload = json.dumps({"type": "NAME_CHANGE", "newName": name}).encode()
        mask = secrets.token_bytes(4)
        head = bytes([0x81]) + (bytes([0x80 | len(payload)]) if len(payload) < 126
                                else bytes([0x80 | 126]) + len(payload).to_bytes(2, "big"))
        sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        time.sleep(0.5)
        sock.sendall(bytes([0x88, 0x80]) + secrets.token_bytes(4))  # close


async def keep_name(user: dict) -> None:
    """Reset a renamed viewer's hall nick to the Twitch nick (at most once per RENAME_EVERY)."""
    record = next((r for r in chatters.values() if r.get("oc_id") == user.get("id")), None)
    if not record or not user.get("displayName") or user["displayName"] == record["name"]:
        return
    if time.time() - renamed.get(record["oc_id"], 0) < RENAME_EVERY:
        return
    renamed[record["oc_id"]] = time.time()
    print(f"gate: ник «{user['displayName']}» в кинозале – обратно {record['name']}", flush=True)
    try:
        await asyncio.to_thread(_ws_name_change, record["oc_token"], record["name"])
    except (OSError, Upstream) as e:
        print(f"gate: ник {record['name']} не вернули: {e}", flush=True)


# --- hall → Twitch as the viewer ---

voices: dict[str, dict] = {}  # Owncast user id → {"twitch_id", "login", "access", "refresh", "expires"}


def remember_voice(oc_id: str, user: dict, tok: dict) -> None:
    old = voices.get(oc_id)
    for other in [k for k, v in voices.items() if v["twitch_id"] == user["id"] and k != oc_id]:
        old = voices.pop(other)  # the same viewer under a previous Owncast user
    voices[oc_id] = {"twitch_id": user["id"], "login": user["login"], "access": tok["access_token"],
                     "refresh": tok["refresh_token"], "expires": time.time() + int(tok.get("expires_in") or 3600)}
    if old:
        _revoke_later([old["access"]])


def forget_voices() -> None:
    """Session over: forget viewer tokens and revoke them with Twitch in the background."""
    gone = [v["access"] for v in voices.values()]
    voices.clear()
    _revoke_later(gone)


def _revoke_later(tokens: list[str]) -> None:
    def run():
        for t in tokens:
            with suppress(Upstream):
                _http("POST", "https://id.twitch.tv/oauth2/revoke", data={"client_id": TWITCH_CLIENT_ID, "token": t})
    if tokens:
        threading.Thread(target=run, daemon=True).start()


async def refresh_voice(v: dict) -> bool:
    status, tok = await http("POST", "https://id.twitch.tv/oauth2/token", data={
        "client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "refresh_token", "refresh_token": v["refresh"]})
    if status != 200 or not tok or not tok.get("access_token"):
        return False
    v.update(access=tok["access_token"], refresh=tok.get("refresh_token") or v["refresh"],
             expires=time.time() + int(tok.get("expires_in") or 3600))
    return True


async def say_as(oc_id: str, text: str) -> None:
    """Send a hall line to Twitch chat as the viewer, prefixed with the mark.

    A Twitch refusal is logged; a token Twitch no longer accepts drops the voice.
    """
    v = voices.get(oc_id)
    if not v or not channel_id:
        return
    message = f"{HALL_MARK} {text}"[:TWITCH_MSG_MAX]
    if v["expires"] - time.time() < 120 and not await refresh_voice(v):
        voices.pop(oc_id, None)
        print(f"gate: hall→twitch: токен {v['login']} не обновился, голос забыт", flush=True)
        return
    for retry in (False, True):
        status, data = await http("POST", "https://api.twitch.tv/helix/chat/messages",
                                  json_body={"broadcaster_id": channel_id, "sender_id": v["twitch_id"],
                                             "message": message},
                                  headers={"Authorization": f"Bearer {v['access']}", "Client-Id": TWITCH_CLIENT_ID})
        if status == 401 and not retry and await refresh_voice(v):
            continue
        break
    if status == 401:
        voices.pop(oc_id, None)
        print(f"gate: hall→twitch: Twitch больше не принимает токен {v['login']}, голос забыт", flush=True)
        return
    sent = ((data or {}).get("data") or [{}])[0] if isinstance(data, dict) else {}
    if status != 200 or not sent.get("is_sent"):
        reason = (sent.get("drop_reason") or {}).get("message") if sent else None
        print(f"gate: hall→twitch: строка {v['login']} не принята (HTTP {status}, {reason or 'без причины'})",
              flush=True)


hall = {"sid": None, "seen": OrderedDict()}


async def relay_once() -> None:
    """One pass: new Owncast chat lines of viewers with a voice go to Twitch.

    The first pass in a session only records what was already said, so old lines are not repeated.
    """
    if not session:
        hall["sid"] = None
        return
    messages = await integration("GET", "/api/integrations/chat")
    sid = session["sid"]
    primed = hall["sid"] == sid
    seen = hall["seen"]
    if not primed:
        seen.clear()
    for m in messages if isinstance(messages, list) else []:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen[mid] = None
        user = m.get("user") or {}
        if primed and not user.get("isBot"):
            await keep_name(user)
        if not primed or m.get("type") != "CHAT" or m.get("visible") is False or user.get("isBot"):
            continue
        if user.get("id") in voices:
            text = plain(m.get("body") or "")
            if text:
                await say_as(user["id"], text)
    while len(seen) > HALL_SEEN_KEEP:
        seen.popitem(last=False)
    hall["sid"] = sid


async def hall_loop() -> None:
    failing = False
    synced = 0.0
    while True:
        await asyncio.sleep(HALL_POLL_SECONDS)
        # Nothing may end this loop: it alone relays the hall, keeps the bans and closes a lapsed lease
        try:
            expire()
            if session and time.time() - synced >= BAN_SYNC_SECONDS:
                synced = time.time()
                await sync_bans()
        except Upstream as e:
            print(f"gate: сверка банов: {e}", flush=True)
        except Exception as e:
            print(f"gate: сверка банов: {e!r}", flush=True)
        try:
            await relay_once()
        except Upstream as e:
            if not failing:
                print(f"gate: hall→twitch: {e}", flush=True)
            failing = True
        except Exception as e:  # one malformed line must not kill the loop
            print(f"gate: hall→twitch: {e!r}", flush=True)
        else:
            failing = False


@app.post(P + "/api/chat")
async def chat_send(req: Request):
    """Post a Twitch line to Owncast chat: {"user": "nick", "body": "text"} or {"body": "nick: text"}.

    Optional "color" (#RRGGBB, the Twitch nick colour), "emotes" ({name: id} of the line's emotes) and
    "badges" (["moderator/1", ...], the author's badges) feed the chat look (GET /_gate/look). Lines of a
    viewer banned in the hall go nowhere: {"sent": false, "banned": true}; the ban is matched by the
    optional "login" too, since the nick is the Twitch display name, which may differ from the login.
    The author's twin «nick [TW]» posts the line with its own colour; on failure or without a nick the
    «Twitch» user posts it. Markup is escaped, @nick mentions are bold.
    """
    if not api_ok(req):
        return Response(status_code=401)
    expire()
    touch()
    try:
        data = json.loads(await req.body() or b"{}")
        body, name, login = data.get("body"), data.get("user"), data.get("login")
        color, emotes, badges = data.get("color"), data.get("emotes"), data.get("badges")
    except (ValueError, AttributeError):
        body = name = login = color = emotes = badges = None
    if not isinstance(body, str) or not body.strip():
        return JSONResponse({"error": "expected {\"body\": \"...\"}"}, status_code=400)
    body = " ".join(body.split())[:CHAT_BODY_MAX]
    if not isinstance(name, str) or not name.strip():
        m = TWITCH_NAME.match(body)
        name, text = (m.group(1), m.group(2)) if m else (None, body)
    else:
        name, text = name.strip(), body
    if (name and banned(login=name)) or (isinstance(login, str) and banned(login=login)):
        return {"sent": False, "banned": True}  # banned in the hall: their Twitch lines are not relayed
    remember_look(name, color, emotes, badges)
    if name:
        try:
            await say_as_twin(name, text)
            return {"sent": True}
        except TwinDisabled:
            return {"sent": False, "banned": True}  # twin banned just now, before the next ban sync
        except TwinUnverified as e:
            # not through the shared «Twitch» user either: the line may come from a viewer banned just now
            print(f"gate: twitch→hall: {e}", flush=True)
            return JSONResponse({"error": str(e)}, status_code=502)
        except Upstream as e:
            print(f"gate: twitch→hall: двойник {name}: {e}, пишет «{CHAT_BOT_NAME}»", flush=True)
    line = f"{escape_markdown(name)}: {hall_markdown(text)}" if name else hall_markdown(text)
    try:
        await integration("POST", "/api/integrations/chat/send", {"body": line})
    except Upstream as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"sent": True}


@app.get(P + "/twitch")
async def twitch_login(req: Request, next: str = "/"):
    nxt = safe_next(next)
    expire()
    if not TWITCH_ENABLED:
        return RedirectResponse(f"{P}/enter?next={quote(nxt)}", status_code=303)
    if not session:
        return page(lang_of(req), closed=True)
    state_ = secrets.token_urlsafe(16)
    query = urlencode({"client_id": TWITCH_CLIENT_ID, "redirect_uri": f"{PUBLIC_URL}{P}/twitch/callback",
                       "response_type": "code", "scope": TWITCH_SCOPES, "state": state_})
    resp = RedirectResponse(f"https://id.twitch.tv/oauth2/authorize?{query}", status_code=303)
    resp.set_cookie(OAUTH_COOKIE, oauth_cookie(state_, nxt), max_age=OAUTH_TTL,
                    httponly=True, secure=True, samesite="lax", path=P)
    return resp


@app.get(P + "/twitch/callback")
async def twitch_callback(req: Request, code: str = "", state: str = "", error: str = ""):
    saved = read_oauth_cookie(req.cookies.get(OAUTH_COOKIE, ""))
    nxt = safe_next(saved[1]) if saved else "/"
    lang = lang_of(req)

    def fail(key: str, status: int = 400) -> HTMLResponse:
        resp = page(lang, nxt=nxt, error=tr(lang, key, chan=TWITCH_CHANNEL), status=status)
        resp.delete_cookie(OAUTH_COOKIE, path=P)
        return resp

    expire()
    if not TWITCH_ENABLED:
        return RedirectResponse(f"{P}/enter", status_code=303)
    if not session:
        return page(lang, closed=True)
    if error:
        return fail("twitch_cancelled")
    if not saved or not code or not same(state, saved[0]):
        return fail("twitch_failed")
    token, kept, sid = None, False, session["sid"]
    try:
        status, tok = await http("POST", "https://id.twitch.tv/oauth2/token", data={
            "client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "code": code,
            "grant_type": "authorization_code", "redirect_uri": f"{PUBLIC_URL}{P}/twitch/callback"})
        if status != 200 or not tok or not tok.get("access_token"):
            return fail("twitch_failed")
        token = tok["access_token"]
        user, chan = await twitch_user(token)
        if banned(user["id"], user["login"]):
            return fail("banned", status=403)
        if not await may_watch(token, user["id"], chan):
            return fail("not_follower", status=403)
        chatter = await chat_identity(user)
        # the hall nick takes its Twitch colour, the same as on the viewer's lines from Twitch
        remember_look(chatter["name"], await viewer_color(token, user["id"]), None,
                      ["broadcaster/1"] if user["id"] == chan and chatter["name"].casefold() not in look["badges"]
                      else None)
        # Writing as the viewer only if they granted the scope, and only for this session: the awaits
        # above may outlast it, and a token kept after forget_voices() would never be revoked
        if ("user:write:chat" in (tok.get("scope") or []) and tok.get("refresh_token")
                and session and session["sid"] == sid):
            remember_voice(chatter["oc_id"], user, tok)
            kept = True
    except Upstream as e:
        print(f"gate: login: {e}", flush=True)
        return fail("unavailable", status=502)
    finally:
        if token and not kept:
            await revoke(token)

    if not session or session["sid"] != sid:
        resp = page(lang, closed=True)
        resp.delete_cookie(OAUTH_COOKIE, path=P)
        return resp
    if cookie_ok(req):
        # code entered earlier: straight to chat under the Twitch nick. The entry cookie is issued
        # again for this account: the old one may name another, and the ban check reads it
        resp = chat_handoff(chatter["oc_token"], with_lang(nxt, lang), lang)
        resp.set_cookie(COOKIE, cookie_value(user["id"]), max_age=COOKIE_TTL,
                        httponly=True, secure=True, samesite="lax", path="/")
        resp.delete_cookie(PENDING_COOKIE, path=P, secure=True, httponly=True, samesite="lax")
    else:
        # Twitch passed, the code is still required
        resp = RedirectResponse(f"{P}/enter?next={quote(nxt)}", status_code=303)
        resp.set_cookie(PENDING_COOKIE, f'{user["id"]}.{pending_sign(user["id"])}', max_age=PENDING_TTL,
                        httponly=True, secure=True, samesite="lax", path=P)
    resp.delete_cookie(OAUTH_COOKIE, path=P)
    return resp


def chat_handoff(oc_token: str, nxt: str, lang: str = LANGS[0]) -> HTMLResponse:
    """Put the Owncast chat token into localStorage (read by the Owncast web client), then open the site."""
    # json.dumps escapes quotes; «</» and «<!--» inside <script> close it, so they are escaped too
    def js(v: str) -> str:
        return json.dumps(v).replace("<", "\\u003c")
    body = (f'<script>try{{localStorage.setItem("accessToken",{js(oc_token)})}}catch(e){{}}'
            f'location.replace({js(nxt)})</script>'
            f'<noscript><a href="{html.escape(nxt)}">{tr(lang, "to_hall")}</a></noscript>')
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">{body}',
                        headers={"Cache-Control": "no-store"})


@app.get(P + "/logout")
async def logout_page(req: Request):
    """The "Log out" button (custom.js): entry cookies cleared, localStorage cleared, then the entry page.

    The page POSTs the chat token first, and the gate revokes this viewer's Twitch token.
    """
    script = ('<script>(function(){var t=null;try{t=localStorage.getItem("accessToken");'
              'localStorage.removeItem("accessToken")}catch(e){}'
              'function go(){location.replace("/_gate/enter")}'
              'if(!t){go();return}'
              'fetch("/_gate/logout",{method:"POST",headers:{"Content-Type":"application/json"},'
              'body:JSON.stringify({token:t})}).then(go,go)})()</script>'
              f'<noscript><a href="/_gate/enter">{tr(lang_of(req), "to_entry")}</a></noscript>')
    resp = HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">{script}',
                        headers={"Cache-Control": "no-store"})
    resp.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    resp.delete_cookie(PENDING_COOKIE, path=P, secure=True, httponly=True, samesite="lax")
    return resp


@app.post(P + "/logout")
async def logout(req: Request):
    """{"token": Owncast chat token}: forget this viewer's voice and revoke their Twitch token."""
    try:
        token = json.loads(await req.body() or b"{}").get("token")
    except (ValueError, AttributeError):
        token = None
    if isinstance(token, str) and token:
        for record in chatters.values():
            if same(record.get("oc_token", ""), token):
                gone = voices.pop(record["oc_id"], None)
                if gone:
                    _revoke_later([gone["access"]])
                break
    return Response(status_code=204)


# --- session API for the bot: open, retitle, query, close ---

def api_ok(req: Request) -> bool:
    return same(req.headers.get("authorization", ""), f"Bearer {API_TOKEN}")


TITLE_MAX = 200


async def read_title(req: Request) -> str | None:
    """Film title from the JSON body `{"title": "..."}`; None when there is no body or no title."""
    raw = await req.body()
    if not raw:
        return None
    try:
        title = json.loads(raw).get("title")
    except (ValueError, AttributeError):
        raise ValueError("the body is not a JSON object") from None
    if title is None:
        return None
    if not isinstance(title, str):
        raise ValueError("title is not a string")
    title = " ".join(title.split())[:TITLE_MAX]
    return title or None


def state() -> dict:
    if not session:
        return {"active": False}
    return {"active": True, "code": session["code"], "started": session["started"],
            "title": session.get("title")}


@app.post(P + "/api/session")
async def session_start(req: Request):
    global session
    if not api_ok(req):
        return Response(status_code=401)
    try:
        title = await read_title(req)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    forget_voices()  # the previous session ends together with the right to write as viewers
    session = {
        "sid": secrets.token_urlsafe(16),
        "code": f"{secrets.randbelow(10 ** CODE_LEN):0{CODE_LEN}d}",
        "started": int(time.time()),
        "title": title,
        "seen": time.time(),
    }
    save_session(session)
    failures.clear()
    return state()


@app.patch(P + "/api/session")
async def session_retitle(req: Request):
    """Retitle the open session; the code and issued cookies stay valid."""
    global session
    if not api_ok(req):
        return Response(status_code=401)
    expire()
    if not session:
        return JSONResponse(state(), status_code=409)
    try:
        title = await read_title(req)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    session = {**session, "title": title, "seen": time.time()}
    save_session(session)
    return state()


@app.get(P + "/api/session")
async def session_status(req: Request):
    if not api_ok(req):
        return Response(status_code=401)
    expire()
    touch()
    return state()


@app.delete(P + "/api/session")
async def session_stop(req: Request):
    global session
    if not api_ok(req):
        return Response(status_code=401)
    expire()
    closed = session is not None
    session = None
    save_session(None)
    forget_voices()
    return {**state(), "closed": closed}


@app.get(P + "/health")
async def health():
    return JSONResponse({"ok": True})


# --- markup ---

def page(lang: str, nxt: str = "/", error: str = "", closed: bool = False, status: int = 200,
         who: str | None = None) -> HTMLResponse:
    def t(key: str) -> str:
        return html.escape(tr(lang, key))
    if closed:
        body = f"""
      <p class="lead">{t("closed_lead")}</p>
      <p class="hint">{t("closed_hint")}</p>"""
    else:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        title = session.get("title") if session else None
        now = f'<p class="now"><span>{t("now")}</span> {html.escape(title)}</p>' if title else ""
        form = f"""
      <form method="post" action="{P}/enter" autocomplete="off">
        <input type="hidden" name="next" value="{html.escape(nxt)}">
        <input class="code" name="code" inputmode="numeric" pattern="[0-9]*"
               maxlength="{CODE_LEN}" placeholder="{'•' * CODE_LEN}"
               autocomplete="one-time-code" autofocus required>
        <button type="submit">{t("enter")}</button>
      </form>
      <p class="hint below">{t("missed")}</p>"""
        if who:
            body = f"""{now}
      <p class="who">Twitch: <b>{html.escape(who)}</b> ✓</p>
      <p class="lead">{t("code_after_twitch")}</p>{form}
      {err}"""
        elif TWITCH_ENABLED:
            chan = (f'<a class="chan" href="https://www.twitch.tv/{quote(TWITCH_CHANNEL)}" target="_blank" '
                    f'rel="noopener">{html.escape(TWITCH_CHANNEL)}</a>')
            body = f"""{now}
      <p class="lead">{t("followers_only").replace("{chan}", chan)}</p>
      <a class="twitch" href="{P}/twitch?next={quote(nxt)}">{TWITCH_ICON}{t("twitch")}</a>
      <p class="hint or">{t("then_code")}</p>
      {err}"""
        else:
            body = f"""{now}
      <p class="lead">{t("code")}</p>{form}
      {err}"""
    # RU | EN: the current language highlighted, the other one a link; each named in itself
    names = {"ru": "Русский", "en": "English"}
    parts = []
    for code in LANGS:
        if code == lang:
            parts.append(f'<span lang="{code}" aria-current="true" title="{names[code]}">{code.upper()}</span>')
        else:
            parts.append(f'<a href="{P}/lang?to={code}&amp;next={quote(nxt)}" lang="{code}" hreflang="{code}" '
                         f'aria-label="{names[code]}" title="{t("switch_label")}">{code.upper()}</a>')
    switch = f'<nav class="lang" aria-label="Язык / Language">{"".join(parts)}</nav>'
    markup = (PAGE.replace("{{LANG}}", lang).replace("{{TITLE}}", t("title"))
              .replace("{{PREVIEW_TITLE}}", html.escape(PREVIEW_TITLE))
              .replace("{{PREVIEW_SUMMARY}}", html.escape(PREVIEW_SUMMARY))
              .replace("{{SWITCH}}", switch).replace("{{BODY}}", body))
    return HTMLResponse(markup, status_code=status, headers={"Cache-Control": "no-store"})


TWITCH_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143'
               'h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428'
               'h-3.429l-3 3v-3H6.857V1.714h13.714Z"/></svg>')

PAGE = """<!doctype html>
<html lang="{{LANG}}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{{TITLE}}</title>
<meta name="description" content="{{PREVIEW_SUMMARY}}">
<meta property="og:title" content="{{PREVIEW_TITLE}}">
<meta property="og:description" content="{{PREVIEW_SUMMARY}}">
<meta property="og:type" content="website">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&family=Unbounded:wght@500;700&display=swap" rel="stylesheet">
<style>
  /* matches the hall (owncast/custom.css): black page, grey #1A1A1A panel with a border, lavender only for accents */
  :root{
    --bg:#000; --panel:#1A1A1A; --field:#111; --line:#333; --line-hi:#555;
    --accent:#BAF; --accent-hi:#DCF; --glow:rgba(136,102,255,.4);
    --text:#EEE; --dim:#AAA; --ink:#111; --err:#F9B;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    display:grid;place-items:center;padding:24px;background:var(--bg);
    color:var(--text);font-family:"JetBrains Mono",ui-monospace,Consolas,monospace;
  }
  .box{
    position:relative;width:min(420px,100%);padding:36px 32px 30px;text-align:center;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;
  }
  h1{
    margin:0 0 20px;font:700 30px/1.1 "Unbounded",system-ui,sans-serif;letter-spacing:.04em;
    color:var(--text);text-shadow:0 0 18px var(--glow);
  }
  .now{margin:0 0 16px;font:700 16px/1.35 "Unbounded",system-ui,sans-serif;color:var(--text);
    text-shadow:0 0 12px var(--glow);overflow-wrap:anywhere}
  .now span{font-weight:500;color:#CCC;text-shadow:none}
  .lead{margin:0 0 22px;font-size:15px;line-height:1.5;color:var(--text)}
  .hint{margin:0;font-size:13px;line-height:1.5;color:var(--dim)}
  .lead,.hint,.now{text-wrap:balance}
  .chan{color:var(--accent);text-decoration:underline;text-decoration-color:rgba(187,170,255,.4);text-underline-offset:3px}
  .chan:hover{color:var(--accent-hi);text-decoration-color:currentColor}
  .chan:focus-visible{outline:2px solid var(--accent-hi);outline-offset:2px;border-radius:2px}
  .hint.below{margin-top:18px}
  .hint.or{margin:12px 0 0}
  form{display:flex;flex-direction:column;gap:12px}
  .code{
    width:100%;padding:14px 10px;text-align:center;
    font:400 34px/1 "JetBrains Mono",monospace;letter-spacing:.4em;text-indent:.4em;
    color:var(--text);background:var(--field);border:1px solid var(--line-hi);border-radius:10px;outline:none;
  }
  .code::placeholder{color:#666}
  .code:focus{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 0 14px var(--glow)}
  button,.twitch{
    display:flex;align-items:center;justify-content:center;gap:10px;
    padding:14px;border:0;border-radius:10px;cursor:pointer;text-decoration:none;
    font:700 15px/1 "Unbounded",system-ui,sans-serif;letter-spacing:.04em;
    color:var(--ink);background:var(--accent);
  }
  .twitch svg{width:18px;height:18px;flex:none;fill:currentColor}
  button:hover,.twitch:hover{background:var(--accent-hi)}
  button:focus-visible,.twitch:focus-visible{outline:2px solid var(--accent-hi);outline-offset:3px}
  .who{display:inline-block;margin:0 0 18px;padding:5px 12px;font-size:13px;color:var(--dim);
    border:1px solid var(--line-hi);border-radius:8px}
  .who b{color:var(--accent);font-weight:500}
  .err{margin:16px 0 0;color:var(--err);font-size:13px;line-height:1.5}
  .lang{position:absolute;top:10px;right:12px;display:flex;border:1px solid var(--line-hi);border-radius:8px;
    font-size:12px;line-height:1.4}
  .lang a,.lang span{padding:4px 8px;color:var(--dim);text-decoration:none}
  .lang span{background:#2A2A2A;color:var(--text);border-radius:7px}
  .lang a:hover,.lang a:focus-visible{color:var(--accent);outline:none}
  @media (pointer:coarse){.lang a,.lang span{padding:10px 12px}}
</style>
</head>
<body>
  <main class="box">
    {{SWITCH}}
    <h1>{{TITLE}}</h1>{{BODY}}
  </main>
</body>
</html>
"""  # noqa: E501 – the fonts link
