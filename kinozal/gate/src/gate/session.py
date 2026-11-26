"""The film session: opened, retitled and closed by the bot, kept open by its lease."""

import secrets
import time

from gate import config, security, state, voices


def start(title: str | None) -> None:
    """Open a new session. The previous one's viewer tokens and wrong-code counts go with it."""
    voices.forget_voices()
    state.session = {
        "sid": secrets.token_urlsafe(16),
        "code": f"{secrets.randbelow(10**config.CODE_LEN):0{config.CODE_LEN}d}",
        "started": int(time.time()),
        "title": title,
        "seen": time.time(),
    }
    state.save_session()
    security.failures.clear()


def retitle(title: str | None) -> None:
    """Retitle the open session. The code and issued cookies stay valid."""
    state.session = {**state.session, "title": title, "seen": time.time()}
    state.save_session()


def close() -> bool:
    """Close the session and forget its viewer tokens. Whether one was open."""
    closed = state.session is not None
    state.session = None
    state.save_session()
    voices.forget_voices()
    return closed


def expire() -> None:
    """Close the session if the bot has not checked in for longer than the lease."""
    if state.session and config.LEASE and time.time() - state.session["seen"] > config.LEASE:
        close()


def touch() -> None:
    """Renew the session lease."""
    if state.session:
        state.session["seen"] = time.time()
        state.save_session()


def info() -> dict:
    """The session as the bot sees it."""
    if not state.session:
        return {"active": False}
    s = state.session
    return {"active": True, "code": s["code"], "started": s["started"], "title": s.get("title")}
