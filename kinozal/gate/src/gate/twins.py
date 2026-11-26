"""Twitch viewers' twins in the hall chat: the integration users «nick [TW]» that post their Twitch lines."""

from gate import chat_markup, config, owncast, state, upstream


class TwinDisabled(upstream.Upstream):
    """A twin is banned in Owncast chat. A fresh token would be rejected too."""


class TwinUnverified(upstream.Upstream):
    """Owncast refused the twin's token and the ban check failed. The author may be banned."""


async def token(name: str, fresh: bool) -> str:
    """Token of the twin «nick [TW]», from the file, from Owncast or new. Send-only scope."""
    key = name.casefold()
    if key not in state.twins or fresh:
        state.twins[key] = await owncast.integration_token(f"{name} {config.TWIN_MARK}".strip(), ["CAN_SEND_MESSAGES"])
        state.save_twins()
    return state.twins[key]


def _twin(message: dict, tail: str) -> dict | None:
    """The twin «nick [TW]» who wrote a history line, or None for anyone else."""
    u = message.get("user") or {}
    shown = str(u.get("displayName") or "")
    return u if config.TWIN_MARK and u.get("id") and shown.endswith(tail) else None


async def in_history() -> tuple[dict[str, str], set[str]]:
    """Banned twins (user id → nick, casefold) and the ids of every twin in the admin chat history.

    Owncast shows a disabled integration only in the admin chat history (user.disabledAt), not among
    disabled users or tokens. A twin whose lines left the history is not seen at all.
    """
    status, history = await owncast.admin("GET", "/api/admin/chat/messages")
    if status != 200 or not isinstance(history, list):
        raise upstream.Upstream(f"owncast chat history: HTTP {status}")
    off, seen, tail = {}, set(), f" {config.TWIN_MARK}"
    for twin in (_twin(m, tail) for m in history):
        if twin:
            seen.add(twin["id"])
            if twin.get("disabledAt"):
                off[twin["id"]] = twin["displayName"][: -len(tail)].strip().casefold()
    return off, seen


async def is_disabled(name: str) -> bool:
    """Whether the twin «nick [TW]» is disabled (banned) in Owncast chat."""
    off, _ = await in_history()
    return name.casefold() in off.values()


async def say(name: str, text: str) -> None:
    """Post a Twitch viewer's line to Owncast chat as their twin.

    A 401 means the token was revoked or the twin was banned. A revoked token is fetched again. A banned
    twin gets no new token, since that would be a new user with the same nick and the ban would not hold.
    """
    for fresh in (False, True):
        twin = await token(name, fresh)
        status, _ = await upstream.http(
            "POST",
            f"{config.OWNCAST_URL}/api/integrations/chat/send",
            json_body={"body": chat_markup.hall_markdown(text)},
            headers={"Authorization": f"Bearer {twin}"},
        )
        if status != 401:
            break
        try:
            disabled = not fresh and await is_disabled(name)
        except upstream.Upstream as e:
            raise TwinUnverified(f"owncast chat/send ({name}): HTTP 401, ban check failed: {e}") from e
        if disabled:
            raise TwinDisabled(f"owncast chat/send ({name}): the twin is disabled in chat")
    if status == 401:
        raise TwinDisabled(f"owncast chat/send ({name}): the twin is disabled in chat")
    if status != 200:
        raise upstream.Upstream(f"owncast chat/send ({name}): HTTP {status}")
