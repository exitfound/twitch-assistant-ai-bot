"""The hall's side of the chat bridge: hall_loop() relays hall lines to Twitch, keeps hall nicks equal to
Twitch ones, syncs the bans and closes a lapsed lease."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Iterator

from gate import chat_markup, config, moderation, owncast, session, state, upstream, voices

HALL_SEEN_KEEP = 200
RENAME_EVERY = 30  # seconds between resets of one viewer's nick
BAN_SYNC_SECONDS = 10

renamed: dict[str, float] = {}  # Owncast user id → when the nick was last reset
hall = {"sid": None, "seen": OrderedDict()}  # the session relayed, and the ids of the lines already seen


async def keep_name(user: dict) -> None:
    """Reset a renamed viewer's hall nick to the Twitch nick (at most once per RENAME_EVERY)."""
    record = next((r for r in state.chatters.values() if r.get("oc_id") == user.get("id")), None)
    if not record or not user.get("displayName") or user["displayName"] == record["name"]:
        return
    if time.time() - renamed.get(record["oc_id"], 0) < RENAME_EVERY:
        return
    renamed[record["oc_id"]] = time.time()
    print(f"gate: hall nick «{user['displayName']}» set back to {record['name']}", flush=True)
    try:
        await asyncio.to_thread(owncast.set_name, record["oc_token"], record["name"])
    except upstream.Upstream as e:
        print(f"gate: nick {record['name']} not set back: {e}", flush=True)


def _unseen(messages, seen: OrderedDict) -> Iterator[dict]:
    """The lines whose ids are not in seen yet, each marked seen as it is handed out.

    A failure while relaying one line leaves the lines after it unmarked, so the next pass takes them.
    """
    for m in messages if isinstance(messages, list) else []:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid and mid not in seen:
            seen[mid] = None
            yield m


async def _pass_on(m: dict) -> None:
    """A new line of a primed session: its author's nick is kept, a viewer's visible chat line goes to Twitch."""
    user = m.get("user") or {}
    if user.get("isBot"):
        return
    await keep_name(user)
    if m.get("type") != "CHAT" or m.get("visible") is False or user.get("id") not in state.voices:
        return
    text = chat_markup.plain(m.get("body") or "")
    if text:
        await voices.say(user["id"], text)


async def relay_once() -> None:
    """Send new Owncast chat lines of viewers with a voice to Twitch.

    The first pass in a session only records what was already said, so old lines are not repeated.
    """
    if not state.session:
        hall["sid"] = None
        return
    messages = await owncast.integration("GET", "/api/integrations/chat")
    sid = state.session["sid"]
    primed, seen = hall["sid"] == sid, hall["seen"]
    if not primed:
        seen.clear()
    for m in _unseen(messages, seen):
        if primed:
            await _pass_on(m)
    while len(seen) > HALL_SEEN_KEEP:
        seen.popitem(last=False)
    hall["sid"] = sid


async def hall_loop() -> None:
    failing = False
    synced = 0.0
    while True:
        await asyncio.sleep(config.HALL_POLL_SECONDS)
        # Nothing may end this loop. It alone relays the hall, keeps the bans and closes a lapsed lease
        try:
            session.expire()
            if state.session and time.time() - synced >= BAN_SYNC_SECONDS:
                synced = time.time()
                await moderation.sync_bans()
        except upstream.Upstream as e:
            print(f"gate: ban sync: {e}", flush=True)
        except Exception as e:
            print(f"gate: ban sync: {e!r}", flush=True)
        try:
            await relay_once()
        except upstream.Upstream as e:
            if not failing:
                print(f"gate: hall→twitch: {e}", flush=True)
            failing = True
        except Exception as e:  # one malformed line must not kill the loop
            print(f"gate: hall→twitch: {e!r}", flush=True)
        else:
            failing = False
