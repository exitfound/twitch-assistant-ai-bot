"""The chat look for custom.js: Twitch nick colours, emotes and badges, from Twitch lines and Helix."""

import re
import time
from urllib.parse import quote

from gate import config, state, twitch, upstream

HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
EMOTE_ID = re.compile(r"^[A-Za-z0-9_]{1,80}$")
EMOTE_NAME = re.compile(r"^\S{1,64}$")
EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/{scale}"
BADGE = re.compile(r"^[a-z0-9_-]{1,40}/[A-Za-z0-9_-]{1,40}$")  # Twitch badge «set/version»
BADGES_MAX = 6
COLORS_MAX = 5000
EMOTES_MAX = 3000
TWITCH_EMOTES_TTL = 6 * 3600  # refresh of global and channel emotes and badges
TWITCH_EMOTES_RETRY = 600

# Global and channel emotes (name → id) and badge images («set/version» → [1x, 2x, title]) from Helix
twitch_emotes: dict = {"at": 0.0, "map": {}, "badges": {}}


def _cap(d: dict, limit: int) -> None:
    while len(d) > limit:
        d.pop(next(iter(d)))


def _put(d: dict, key: str, value) -> bool:
    """Make value the newest entry of key, or drop key when value is falsy. Whether anything changed."""
    if d.get(key) == (value or None):
        return False
    d.pop(key, None)
    if value:
        d[key] = value
    return True


def _valid_badges(badges: list) -> list[str]:
    return [b for b in badges if isinstance(b, str) and BADGE.match(b)][:BADGES_MAX]


def _valid_emotes(emotes) -> list[tuple[str, str]]:
    """The well-formed (name, id) pairs among a line's first 50 emotes."""
    if not isinstance(emotes, dict):
        return []
    pairs = list(emotes.items())[:50]
    return [
        (n, i)
        for n, i in pairs
        if isinstance(n, str) and isinstance(i, str) and EMOTE_NAME.match(n) and EMOTE_ID.match(i)
    ]


def remember(name: str | None, color, emotes, badges=None) -> None:
    """Remember the nick colour, emotes and badges of a Twitch line from the bot. Malformed values are skipped."""
    look, changed = state.look, False
    if name and isinstance(badges, list):
        changed |= _put(look["badges"], name.casefold(), _valid_badges(badges))
    if name and isinstance(color, str) and HEX_COLOR.match(color):
        changed |= _put(look["colors"], name.casefold(), color.upper())
    for em_name, em_id in _valid_emotes(emotes):
        changed |= _put(look["emotes"], em_name, em_id)
    if changed:
        _cap(look["colors"], COLORS_MAX)
        _cap(look["emotes"], EMOTES_MAX)
        _cap(look["badges"], COLORS_MAX)
        state.save_look()


async def _helix_list(url: str, hdrs: dict) -> list:
    status, data = await upstream.http("GET", url, headers=hdrs)
    if status != 200 or not data or not isinstance(data.get("data"), list):
        raise upstream.Upstream(f"{url.split('?')[0].split('/helix/')[1]}: HTTP {status}")
    return data["data"]


async def _fetch_emotes(hdrs: dict, chan: str) -> dict:
    """Global and channel emotes, name → id."""
    found = {}
    for url in (
        "https://api.twitch.tv/helix/chat/emotes/global",
        f"https://api.twitch.tv/helix/chat/emotes?broadcaster_id={chan}",
    ):
        for e in await _helix_list(url, hdrs):
            if EMOTE_NAME.match(str(e.get("name", ""))) and EMOTE_ID.match(str(e.get("id", ""))):
                found[e["name"]] = e["id"]
    return found


def _badge_entry(set_id, version: dict) -> tuple[str, list] | None:
    """A badge's «set/version» and [1x image, 2x image, title]; None if malformed or its image is not on https."""
    key, one = f"{set_id}/{version.get('id')}", str(version.get("image_url_1x", ""))
    if not BADGE.match(key) or not one.startswith("https://"):
        return None
    return key, [one, version.get("image_url_2x") or one, str(version.get("title") or "")[:60]]


async def _fetch_badges(hdrs: dict, chan: str) -> dict:
    """Global and channel badge images, «set/version» → [1x, 2x, title]. A channel badge overrides a global one."""
    badges = {}
    for url in (
        "https://api.twitch.tv/helix/chat/badges/global",
        f"https://api.twitch.tv/helix/chat/badges?broadcaster_id={chan}",
    ):
        for badge_set in await _helix_list(url, hdrs):
            for version in badge_set.get("versions") or []:
                entry = _badge_entry(badge_set.get("set_id"), version)
                if entry:
                    badges[entry[0]] = entry[1]
    return badges


async def channel_emotes() -> dict:
    """Twitch global and channel emotes (name → id), refreshed every TWITCH_EMOTES_TTL.

    Also fills twitch_emotes["badges"] with global and channel badge images.
    """
    now = time.time()
    if not config.TWITCH_ENABLED or now - twitch_emotes["at"] < TWITCH_EMOTES_TTL:
        return twitch_emotes["map"]
    twitch_emotes["at"] = now - TWITCH_EMOTES_TTL + TWITCH_EMOTES_RETRY  # on failure, retry in 10 minutes
    try:
        hdrs = twitch.helix(await twitch.app_access_token())
        chan = await twitch.find_channel_id(hdrs)
        found = await _fetch_emotes(hdrs, chan)
        badges = await _fetch_badges(hdrs, chan)
    except upstream.Upstream as e:
        print(f"gate: Twitch emotes and badges: {e}", flush=True)
        return twitch_emotes["map"]
    twitch_emotes.update(at=now, map=found, badges=badges)
    return found


async def viewer_color(token: str, user_id: str) -> str | None:
    """The viewer's Twitch nick colour, read with their token. None if unset or on error."""
    try:
        status, data = await upstream.http(
            "GET", f"https://api.twitch.tv/helix/chat/color?user_id={quote(user_id)}", headers=twitch.helix(token)
        )
    except upstream.Upstream:
        return None
    if status != 200 or not data or not data.get("data"):
        return None
    color = data["data"][0].get("color") or ""
    return color if HEX_COLOR.match(color) else None
