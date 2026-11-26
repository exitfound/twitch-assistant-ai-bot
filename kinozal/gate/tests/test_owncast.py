"""Owncast's API as the gate reads it: integration tokens, the shared «Twitch» sender, chat users of viewers."""

import asyncio

import pytest


def owncast_answers(gate, monkeypatch, answers: dict) -> list:
    """Owncast answers each path from answers (a (status, body), or a list of them in turn); returns the calls."""
    calls = []

    async def fake(method, url, **kw):
        path = url.removeprefix(gate.config.OWNCAST_URL).split("?")[0]
        calls.append((method, path, kw.get("headers", {}).get("Authorization", "")))
        answer = answers[path]
        return answer.pop(0) if isinstance(answer, list) else answer

    monkeypatch.setattr(gate.upstream, "http", fake)
    return calls


def created(calls) -> int:
    return sum(path == "/api/admin/accesstokens/create" for _, path, _ in calls)


@pytest.mark.parametrize(
    "tokens, reused",
    [
        ([{"displayName": "Twitch", "scopes": ["CAN_SEND_MESSAGES", "HAS_ADMIN_ACCESS"], "accessToken": "old"}], True),
        (
            [{"displayName": "Twitch", "scopes": ["CAN_SEND_MESSAGES", "HAS_ADMIN_ACCESS", "X"], "accessToken": "old"}],
            True,
        ),
        ([{"displayName": "Twitch", "scopes": ["CAN_SEND_MESSAGES"], "accessToken": "old"}], False),
        ([{"displayName": "Twitch", "accessToken": "old"}], False),
        ([{"displayName": "Other", "scopes": ["CAN_SEND_MESSAGES", "HAS_ADMIN_ACCESS"], "accessToken": "old"}], False),
    ],
)
def test_an_integration_token_is_reused_only_with_its_name_and_every_scope(gate, monkeypatch, tokens, reused):
    calls = owncast_answers(
        gate,
        monkeypatch,
        {"/api/admin/accesstokens": (200, tokens), "/api/admin/accesstokens/create": (200, {"accessToken": "new"})},
    )
    token = asyncio.run(gate.owncast.integration_token("Twitch", gate.owncast.CHAT_SCOPES))
    assert token == ("old" if reused else "new") and created(calls) == (0 if reused else 1)


@pytest.mark.parametrize(
    "answers",
    [
        {"/api/admin/accesstokens": (200, {"error": "no list"})},
        {"/api/admin/accesstokens": (200, []), "/api/admin/accesstokens/create": (200, {})},
        {"/api/admin/accesstokens": (200, []), "/api/admin/accesstokens/create": (200, None)},
        {"/api/admin/accesstokens": (200, []), "/api/admin/accesstokens/create": (500, {"accessToken": "x"})},
    ],
)
def test_an_unusable_token_answer_is_an_upstream_failure(gate, monkeypatch, answers):
    owncast_answers(gate, monkeypatch, answers)
    with pytest.raises(gate.upstream.Upstream):
        asyncio.run(gate.owncast.integration_token("Twitch", gate.owncast.CHAT_SCOPES))


def test_the_integrations_api_uses_the_cached_token_and_takes_a_fresh_one_once_on_a_401(gate, monkeypatch):
    gate.owncast.chat_token = "cached"
    calls = owncast_answers(
        gate,
        monkeypatch,
        {
            "/api/integrations/chat": [(200, []), (401, None), (200, ["line"])],
            "/api/admin/accesstokens": (200, []),
            "/api/admin/accesstokens/create": (200, {"accessToken": "fresh"}),
        },
    )
    assert asyncio.run(gate.owncast.integration("GET", "/api/integrations/chat")) == []
    assert asyncio.run(gate.owncast.integration("GET", "/api/integrations/chat")) == ["line"]
    sent = [auth for _, path, auth in calls if path == "/api/integrations/chat"]
    assert sent == ["Bearer cached", "Bearer cached", "Bearer fresh"]


def test_two_401s_in_a_row_are_an_upstream_failure(gate, monkeypatch):
    gate.owncast.chat_token = "cached"
    owncast_answers(
        gate,
        monkeypatch,
        {
            "/api/integrations/chat": (401, None),
            "/api/admin/accesstokens": (200, []),
            "/api/admin/accesstokens/create": (200, {"accessToken": "fresh"}),
        },
    )
    with pytest.raises(gate.upstream.Upstream):
        asyncio.run(gate.owncast.integration("GET", "/api/integrations/chat"))


VIEWER = {"id": "7", "login": "gopnik", "display_name": "Gopnik"}


@pytest.mark.parametrize("answer", [(200, None), (200, {"id": "oc7"}), (500, {"id": "oc7", "accessToken": "t"})])
def test_an_unusable_chat_registration_is_an_upstream_failure(gate, monkeypatch, answer):
    owncast_answers(gate, monkeypatch, {"/api/chat/register": answer})
    with pytest.raises(gate.upstream.Upstream):
        asyncio.run(gate.owncast.chat_identity(VIEWER))
    assert gate.state.chatters == {}


@pytest.mark.parametrize("answer", [(200, {"error": "no list"}), (500, [])])
def test_a_refused_chat_token_with_no_readable_ban_list_is_an_upstream_failure(gate, monkeypatch, answer):
    """A 401 on a known viewer's token is checked against the disabled users. Unread, it must not register anew."""
    gate.state.chatters["7"] = {"login": "gopnik", "name": "Gopnik", "oc_id": "oc7", "oc_token": "t", "sid": "old"}
    calls = owncast_answers(gate, monkeypatch, {"/api/chat": (401, None), "/api/admin/chat/users/disabled": answer})
    with pytest.raises(gate.upstream.Upstream):
        asyncio.run(gate.owncast.chat_identity(VIEWER))
    assert all(path != "/api/chat/register" for _, path, _ in calls)


REGISTERED = (200, {"id": "oc-new", "accessToken": "token-new"})


@pytest.mark.parametrize(
    "display_name, shown",
    [("GoPnIk", "GoPnIk"), ("Вася", "gopnik"), (None, "gopnik"), ("", "gopnik")],
)
def test_the_hall_name_is_the_twitch_display_name_unless_it_is_another_spelling(gate, monkeypatch, display_name, shown):
    """A display name in another script, or none, shows as the login: the hall nick must read as the Twitch one."""
    calls = owncast_answers(gate, monkeypatch, {"/api/chat/register": REGISTERED})
    record = asyncio.run(gate.owncast.chat_identity({"id": "7", "login": "gopnik", "display_name": display_name}))
    assert record["name"] == shown and gate.state.chatters["7"] == record
    assert [path for _, path, _ in calls] == ["/api/chat/register"]


@pytest.mark.parametrize("same_session", [True, False])
def test_a_working_chat_user_is_kept_within_its_session_and_replaced_in_a_new_one(gate, monkeypatch, same_session):
    gate.session.start(None)
    sid = gate.state.session["sid"] if same_session else "an-older-session"
    known = {"login": "gopnik", "name": "Gopnik", "oc_id": "oc7", "oc_token": "t", "sid": sid}
    gate.state.chatters["7"] = known
    owncast_answers(gate, monkeypatch, {"/api/chat": (200, {}), "/api/chat/register": REGISTERED})
    record = asyncio.run(gate.owncast.chat_identity(VIEWER))
    assert (record is known) is same_session
    assert record["oc_id"] == ("oc7" if same_session else "oc-new")


def test_a_revoked_twin_token_is_taken_again_once_the_twin_proves_not_banned(gate, monkeypatch):
    """A 401 on a twin is checked for a ban first; not banned, the twin gets a fresh token and posts."""
    gate.state.twins["gopnik"] = "revoked"
    calls = owncast_answers(
        gate,
        monkeypatch,
        {
            "/api/integrations/chat/send": [(401, None), (200, {})],
            "/api/admin/chat/messages": (200, []),
            "/api/admin/accesstokens": (200, []),
            "/api/admin/accesstokens/create": (200, {"accessToken": "fresh"}),
        },
    )
    asyncio.run(gate.twins.say("Gopnik", "hello"))
    sent = [auth for _, path, auth in calls if path == "/api/integrations/chat/send"]
    assert sent == ["Bearer revoked", "Bearer fresh"] and gate.state.twins["gopnik"] == "fresh"
