"""The gate's state: the session, the chatters, the viewers' voices, the bans, the chat look and the twins.

Every handler is async, so all of it changes on the event loop only: a sync handler would run in a worker
thread and race the loop (a shared .tmp file, the session gone mid-request). Everything but the voices is
kept in /data and read back at start. Modules read and assign it as state.NAME.
"""

import time

from gate import config, storage


def _load_session() -> dict | None:
    loaded = storage.read_json(config.STATE_FILE, None)
    if loaded:
        loaded.setdefault("seen", time.time())
    return loaded


def _load_look() -> dict:
    data = storage.read_json(config.LOOK_FILE, {})
    try:
        return {
            "colors": dict(data.get("colors") or {}),
            "emotes": dict(data.get("emotes") or {}),
            "badges": dict(data.get("badges") or {}),
        }
    except (ValueError, AttributeError):
        return {"colors": {}, "emotes": {}, "badges": {}}


def _load_bans() -> dict:
    data = storage.read_json(config.BANS_FILE, {})
    return data if isinstance(data, dict) else {}


# {"sid", "code", "started", "title", "seen"}, or None while no session is open
session: dict | None = _load_session()
# Twitch id → {"login", "name", "oc_id", "oc_token", "sid"}
chatters: dict[str, dict] = storage.read_json(config.CHATTERS_FILE, {})
# Owncast user id → {"twitch_id", "login", "access", "refresh", "expires"}. Memory only, for the session
voices: dict[str, dict] = {}
# "id:<Twitch id>" | "tw:<nick>" → {"login", "name", "oc_ids": [...], "at"}
bans: dict[str, dict] = _load_bans()
# colors: nick (casefold) → #RRGGBB; emotes: name → id, seen in Twitch chat;
# badges: nick (casefold) → Twitch chat badges, ["moderator/1", "subscriber/12", ...]
look: dict[str, dict] = _load_look()
# Twitch nick (casefold) → Owncast integration token of its twin «nick [TW]»
twins: dict[str, str] = storage.read_json(config.TWINS_FILE, {})


def save_session() -> None:
    storage.write_json(config.STATE_FILE, session)


def save_chatters() -> None:
    storage.write_json(config.CHATTERS_FILE, chatters)


def save_bans() -> None:
    storage.write_json(config.BANS_FILE, bans)


def save_look() -> None:
    storage.write_json(config.LOOK_FILE, look)


def save_twins() -> None:
    storage.write_json(config.TWINS_FILE, twins)
