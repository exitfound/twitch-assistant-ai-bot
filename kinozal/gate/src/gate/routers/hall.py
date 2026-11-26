"""The hall chat's helpers for custom.js: the chat look, who is in chat, and the streamer's «Bans» window."""

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gate import access, config, look, moderation, owncast, state, upstream
from gate.routers import body

router = APIRouter(prefix=config.P)

USERS_TTL = 5  # open "Chat members" panels query Owncast at most once per 5 s in total
# A failure is cached too. With Owncast hung, every request would wait out the timeout in the thread pool
# that the chat bridge and ban sync share. One Owncast request runs at a time, the rest wait for its answer
users_cache: dict = {"at": 0.0, "users": None}
users_lock = asyncio.Lock()


@router.get("/look")
async def chat_look(req: Request):
    """Twitch nick colours, badges and emotes for custom.js. Emotes are global, channel and seen in chat."""
    if not (access.host_ok(req) or access.cookie_ok(req)):
        return Response(status_code=401)
    seen = state.look
    emotes = {**await look.channel_emotes(), **seen["emotes"]}
    used = {b for badges in seen["badges"].values() for b in badges}
    known = look.twitch_emotes["badges"]
    images = {b: known[b] for b in used if b in known}
    return JSONResponse(
        {
            "colors": seen["colors"],
            "emotes": emotes,
            "url": look.EMOTE_URL,
            "badges": seen["badges"],
            "badgeImages": images,
        },
        headers={"Cache-Control": "private, max-age=20"},
    )


@router.get("/users")
async def users(req: Request):
    """Who is in the hall chat, for the member list and the «@» hint in custom.js.

    Signed-in viewers only. 502 if Owncast does not respond.
    """
    if not (access.host_ok(req) or access.cookie_ok(req)):
        return Response(status_code=401)
    async with users_lock:
        if not users_cache["at"] or time.monotonic() - users_cache["at"] > USERS_TTL:
            try:
                status, clients = await owncast.admin("GET", "/api/admin/chat/clients")
            except upstream.Upstream:
                status, clients = 0, None
            listed = owncast.chat_users(clients) if status == 200 else None
            users_cache.update(at=time.monotonic(), users=listed)
    if users_cache["users"] is None:
        return Response(status_code=502)
    return JSONResponse({"users": users_cache["users"]}, headers={"Cache-Control": "private, max-age=5"})


@router.get("/bans")
async def bans_list(req: Request):
    """Who is banned, newest first, for the "Bans" window in custom.js. Streamer only.

    Syncs with Owncast first, so a ban made a second ago is listed. If Owncast does not respond,
    the list comes from the file.
    """
    if not access.streamer_ok(req):
        return Response(status_code=403)
    try:
        await moderation.sync_bans()
    except upstream.Upstream as e:
        print(f"gate: ban sync: {e}", flush=True)
    tail = f" {config.TWIN_MARK}"
    items = [
        {
            "key": k,
            "name": str(v.get("name") or v.get("login") or "").removesuffix(tail),
            "twitch": k.startswith("tw:"),
            "at": v.get("at") or 0,
        }
        for k, v in state.bans.items()
    ]
    items.sort(key=lambda x: -x["at"])
    return JSONResponse({"bans": items}, headers={"Cache-Control": "no-store"})


@router.post("/unban")
async def unban(req: Request):
    """Lift a ban by {"key": key from /_gate/bans}. Streamer only. 502 if Owncast fails, and the ban stays."""
    if not access.streamer_ok(req):
        return Response(status_code=403)
    key = (await body.json_object(req) or {}).get("key")
    try:
        lifted = isinstance(key, str) and await moderation.unban(key)
    except upstream.Upstream as e:
        print(f"gate: {e}", flush=True)
        return Response(status_code=502)
    return Response(status_code=204 if lifted else 404)
