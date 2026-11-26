"""Bans by Twitch account: synced with the users disabled in Owncast chat, lifted from the hall's «Bans» window."""

import asyncio
import time

from gate import config, owncast, state, twins, upstream

# Ban sync and unban take turns. A sync started before an unban would restore the lifted ban
bans_lock = asyncio.Lock()


def banned(twitch_id: str | None = None, login: str | None = None) -> bool:
    if twitch_id and f"id:{twitch_id}" in state.bans:
        return True
    key = login.casefold() if login else None
    return bool(key) and any(b.get("login") == key for b in state.bans.values())


async def sync_bans() -> None:
    """Sync bans with the users disabled in Owncast chat, under the bans lock."""
    async with bans_lock:
        await _sync_bans()


async def _sync_bans() -> None:
    """Record a new ban by Twitch account, forget a lifted one.

    A hall viewer is matched by their chat user (chatters), a Twitch viewer by the twin «nick [TW]».
    A ban is lifted in the hall chat's "Bans" window (`POST /_gate/unban`) or in the Owncast admin.
    The admin can lift a twin's ban only while the twin is still in the chat history.
    """
    users, off, twins_seen = await _disabled()
    by_oc = {r.get("oc_id"): (tid, r) for tid, r in state.chatters.items()}
    changed = False
    for user in users:
        found = _ban_key(user, by_oc)
        if found:
            changed |= _record_ban(*found, user)
    changed |= _forget_lifted(off, twins_seen)
    if changed:
        state.save_bans()


async def _disabled() -> tuple[list[dict], set, set]:
    """Owncast's disabled chat users with the banned twins added, the ids of both, the ids of every twin seen."""
    status, disabled = await owncast.admin("GET", "/api/admin/chat/users/disabled")
    if status != 200 or not isinstance(disabled, list):
        raise upstream.Upstream(f"owncast disabled users: HTTP {status}")
    mark = config.TWIN_MARK
    twins_off, twins_seen = await twins.in_history() if mark else ({}, set())
    off = {d.get("id") for d in disabled} | set(twins_off)
    users = list(disabled) + [{"id": i, "displayName": f"{login} {mark}"} for i, login in twins_off.items()]
    return users, off, twins_seen


def _ban_key(user: dict, by_oc: dict) -> tuple[str, str] | None:
    """A disabled user's ban key and login: "id:<Twitch id>" for a hall viewer, "tw:<nick>" for a twin."""
    oc_id, shown, mark = user.get("id"), str(user.get("displayName") or ""), config.TWIN_MARK
    if oc_id in by_oc:
        tid, record = by_oc[oc_id]
        return f"id:{tid}", str(record.get("login", "")).casefold()
    if mark and shown.endswith(f" {mark}"):
        login = shown.removesuffix(mark).strip().casefold()
        return f"tw:{login}", login
    return None


def _record_ban(key: str, login: str, user: dict) -> bool:
    """Add a new ban, or a new chat user of a banned account. Whether bans changed."""
    oc_id, entry = user.get("id"), state.bans.get(key)
    if entry is None:
        # name is the chat nick at ban time. It labels the IP ban («Banning of <nick>») that unban removes
        shown = str(user.get("displayName") or "")
        state.bans[key] = {"login": login, "name": shown, "oc_ids": [oc_id], "at": int(time.time())}
        print(f"gate: banned in the hall: {login}", flush=True)
        return True
    if oc_id not in entry["oc_ids"]:
        entry["oc_ids"].append(oc_id)
        return True
    return False


def _forget_lifted(off: set, twins_seen: set) -> bool:
    """Forget the bans none of whose chat users is disabled any more. Whether bans changed."""
    changed = False
    for key, entry in list(state.bans.items()):
        ids = entry.get("oc_ids", [])
        if any(i in off for i in ids):
            continue
        # A twin is visible only in the history. One gone from it keeps its ban until an unban
        if key.startswith("tw:") and not any(i in twins_seen for i in ids):
            continue
        del state.bans[key]
        print(f"gate: ban lifted: {entry.get('login')}", flush=True)
        changed = True
    return changed


async def unban(key: str) -> bool:
    """Lift a ban: enable its chat users, lift its IP bans and forget it. False if there is no such ban.

    When Owncast bans a connected viewer, it also bans their IPs with the note «Banning of <nick>».
    Enabling the user does not lift that, and the viewer would stay out of chat. If Owncast fails,
    Upstream is raised and the ban stays.
    """
    async with bans_lock:
        entry = state.bans.get(key)
        if entry is None:
            return False
        try:
            await _lift(entry)
        except upstream.Upstream as e:
            raise upstream.Upstream(f"unban of {entry.get('login')}: {e}") from e
        del state.bans[key]
        state.save_bans()
    print(f"gate: ban lifted from the hall: {entry.get('login')}", flush=True)
    return True


async def _lift(entry: dict) -> None:
    for oc_id in entry.get("oc_ids", []):
        status, _ = await owncast.admin("POST", "/api/admin/chat/users/setenabled", {"userId": oc_id, "enabled": True})
        if status != 200:
            raise upstream.Upstream(f"owncast setenabled: HTTP {status}")
    await _lift_ip_bans({f"banning of {n}".casefold() for n in (entry.get("name"), entry.get("login")) if n})


async def _lift_ip_bans(notes: set[str]) -> None:
    """Remove the IP bans whose note, casefolded, is one of these («banning of <nick>»)."""
    status, ips = await owncast.admin("GET", "/api/admin/chat/users/ipbans")
    if status != 200 or not isinstance(ips, list):
        raise upstream.Upstream(f"owncast ipbans: HTTP {status}")
    for ban in ips:
        if isinstance(ban, dict) and str(ban.get("notes") or "").casefold() in notes and ban.get("ipAddress"):
            status, _ = await owncast.admin("POST", "/api/admin/chat/users/ipbans/remove", {"value": ban["ipAddress"]})
            if status != 200:
                raise upstream.Upstream(f"owncast ipbans/remove: HTTP {status}")
