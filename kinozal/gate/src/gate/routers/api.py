"""The bot's API, Bearer GATE_API_TOKEN: the session, opened, retitled, checked and closed, and Twitch lines
into the hall. A contract with the running bot (src/twitch/local/film/): a change keeps it working."""

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gate import access, chat_markup, config, look, moderation, owncast, session, twins, upstream
from gate.routers import body

router = APIRouter(prefix=config.P)

TITLE_MAX = 200
CHAT_BODY_MAX = 500
TWITCH_NAME = re.compile(r"^(\w{1,25}): (.+)$", re.S)


async def read_title(req: Request) -> str | None:
    """Film title from the JSON body `{"title": "..."}`, or None without a body or title. ValueError if malformed."""
    data = await body.json_object(req)
    if data is None:
        raise ValueError("the body is not a JSON object")
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("title is not a string")
    if not title:
        return None
    return " ".join(title.split())[:TITLE_MAX] or None


@router.post("/api/session")
async def session_start(req: Request):
    if not access.api_ok(req):
        return Response(status_code=401)
    try:
        title = await read_title(req)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    session.start(title)
    return session.info()


@router.patch("/api/session")
async def session_retitle(req: Request):
    """Retitle the open session. The code and issued cookies stay valid."""
    if not access.api_ok(req):
        return Response(status_code=401)
    session.expire()
    if not session.info()["active"]:
        return JSONResponse(session.info(), status_code=409)
    try:
        title = await read_title(req)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # The lease may lapse while the body is read
    if not session.info()["active"]:
        return JSONResponse(session.info(), status_code=409)
    session.retitle(title)
    return session.info()


@router.get("/api/session")
async def session_status(req: Request):
    if not access.api_ok(req):
        return Response(status_code=401)
    session.expire()
    session.touch()
    return session.info()


@router.delete("/api/session")
async def session_stop(req: Request):
    if not access.api_ok(req):
        return Response(status_code=401)
    session.expire()
    closed = session.close()
    return {**session.info(), "closed": closed}


def _author(text: str, user) -> tuple[str | None, str]:
    """The line's author and text: "user" when given, else a leading «nick: » of the text."""
    if isinstance(user, str) and user.strip():
        return user.strip(), text
    m = TWITCH_NAME.match(text)
    return (m.group(1), m.group(2)) if m else (None, text)


def _banned(name: str | None, login) -> bool:
    """Banned in the hall by the display name or by the optional login, which an older bot does not send."""
    return bool(name and moderation.banned(login=name)) or (isinstance(login, str) and moderation.banned(login=login))


async def _as_twin(name: str, text: str):
    """The answer once the author's twin posted the line or must not, or None to post as the shared «Twitch» user."""
    try:
        await twins.say(name, text)
        return {"sent": True}
    except twins.TwinDisabled:
        return {"sent": False, "banned": True}  # twin banned just now, before the next ban sync
    except twins.TwinUnverified as e:
        # No fallback to the shared «Twitch» user. The author may have been banned just now
        print(f"gate: twitch→hall: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=502)
    except upstream.Upstream as e:
        print(f"gate: twitch→hall: twin {name}: {e}, posting as «{owncast.CHAT_BOT_NAME}»", flush=True)
        return None


async def _as_shared(name: str | None, text: str):
    text = chat_markup.hall_markdown(text)
    line = f"{chat_markup.escape_markdown(name)}: {text}" if name else text
    try:
        await owncast.integration("POST", "/api/integrations/chat/send", {"body": line})
    except upstream.Upstream as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"sent": True}


@router.post("/api/chat")
async def chat_send(req: Request):
    """Post a Twitch line to Owncast chat, {"user": "nick", "body": "text"} or {"body": "nick: text"}.

    Optional "color" (#RRGGBB), "emotes" ({name: id}) and "badges" (["moderator/1", ...]) feed the
    chat look. A viewer banned in the hall gets {"sent": false, "banned": true}. The ban is matched by
    the optional "login" too, since "user" is the display name. The author's twin «nick [TW]» posts
    the line, or the «Twitch» user on failure or without a nick.
    """
    if not access.api_ok(req):
        return Response(status_code=401)
    session.expire()
    session.touch()
    data = await body.json_object(req) or {}
    text = data.get("body")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"error": 'expected {"body": "..."}'}, status_code=400)
    name, text = _author(" ".join(text.split())[:CHAT_BODY_MAX], data.get("user"))
    if _banned(name, data.get("login")):
        return {"sent": False, "banned": True}  # banned in the hall, so their Twitch lines are not relayed
    look.remember(name, data.get("color"), data.get("emotes"), data.get("badges"))
    answer = await _as_twin(name, text) if name else None
    return answer if answer is not None else await _as_shared(name, text)


@router.get("/health")
async def health():
    return JSONResponse({"ok": True})
