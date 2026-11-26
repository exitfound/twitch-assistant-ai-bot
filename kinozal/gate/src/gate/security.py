"""Signatures, secret comparison, the client's address, the brute-force limits and the return address."""

import hashlib
import hmac
import time

from fastapi import Request

from gate import config

FAIL_LIMIT, FAIL_WINDOW = 10, 600  # wrong codes per 10 minutes, per Twitch account (per IP without Twitch)
FAIL_IP_LIMIT = 50  # per IP with Twitch. Viewers behind one carrier NAT share an address

# address or "tw:<Twitch id>" → times of wrong codes and host keys within FAIL_WINDOW
failures: dict[str, list[float]] = {}


def sign(value: str) -> str:
    return hmac.new(config.SECRET, value.encode(), hashlib.sha256).hexdigest()


# The streamer's permanent dock pass. Valid until GATE_SECRET changes
HOST_KEY = hmac.new(config.SECRET, b"host-dock", hashlib.sha256).hexdigest()[:32]


def same(a: str, b: str) -> bool:
    """Constant-time equality. compare_digest raises on a non-ASCII str, and a client may send one."""
    return hmac.compare_digest(a.encode(), b.encode())


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
    """A path on this site, else "/".

    Control characters and spaces are refused. Browsers and urlparse drop a tab or a newline,
    so «/<tab>/evil.com» would become the outside address «//evil.com».
    """
    outside = not n.startswith("/") or n[1:2] in ("/", "\\") or n.startswith(config.P)
    if outside or any(c <= " " or c == "\x7f" for c in n):
        return "/"
    return n
