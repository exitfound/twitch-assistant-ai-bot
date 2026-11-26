"""The gate's settings: the environment, read once at import, and the values several modules share.

Modules read them as config.NAME at call time, so a test patches one place.
"""

import os
from pathlib import Path

SECRET = os.environ["GATE_SECRET"].encode()
API_TOKEN = os.environ["GATE_API_TOKEN"]
CODE_LEN = int(os.environ.get("GATE_CODE_LEN", "4"))
LEASE = int(os.environ.get("GATE_LEASE_SECONDS", "180"))
PUBLIC_URL = os.environ["GATE_PUBLIC_URL"].rstrip("/")

STATE_FILE = Path(os.environ.get("GATE_STATE", "/data/session.json"))
CHATTERS_FILE = STATE_FILE.with_name("chatters.json")
TWINS_FILE = STATE_FILE.with_name("twins.json")
LOOK_FILE = STATE_FILE.with_name("look.json")
BANS_FILE = STATE_FILE.with_name("bans.json")

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
TWITCH_CHANNEL = os.environ["TWITCH_CHANNEL"].lower()
TWITCH_ENABLED = bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)

OWNCAST_URL = os.environ.get("OWNCAST_URL", "http://owncast:8080").rstrip("/")
OWNCAST_ADMIN_PASSWORD = os.environ.get("OWNCAST_ADMIN_PASSWORD", "")

HALL_MARK = os.environ.get("HALL_MARK", "🎬 [KZ]")
TWIN_MARK = os.environ.get("TWIN_MARK", "[TW]")
HALL_POLL_SECONDS = float(os.environ.get("HALL_POLL_SECONDS", "2"))

P = "/_gate"
HTTP_TIMEOUT = 8

COOKIE = "kz"  # the entry: the session and the Twitch account, signed
COOKIE_TTL = 12 * 3600
HOST_COOKIE = "kzh"  # the streamer's permanent dock pass
HOST_TTL = 365 * 24 * 3600
PENDING_COOKIE = "kzp"  # set after Twitch, until the code is entered
PENDING_TTL = 1800
LANG_COOKIE = "kz_lang"  # the viewer's choice. custom.js reads it too, so not HttpOnly
LANG_TTL = 365 * 24 * 3600
