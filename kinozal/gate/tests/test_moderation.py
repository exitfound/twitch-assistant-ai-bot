"""Bans by Twitch account, the streamer's «Bans» window and the chat moderator."""

import asyncio
import json
import os
import time

import pytest
from websockets.sync.client import connect

from helpers import (
    CHANNEL_ID,
    OWNCAST,
    admin,
    bot,
    by,
    chat,
    hall_history,
    handoff_token,
    login,
    needs_owncast,
    twitch_step,
)


def test_a_banned_viewer_s_twitch_lines_stay_out_of_the_hall(client, gate, monkeypatch):
    said = []
    monkeypatch.setattr(gate.twins, "say", lambda name, text: asyncio.sleep(0, said.append(name)))
    gate.state.bans["tw:gopnik"] = {"login": "gopnik", "oc_ids": ["x"], "at": 0}
    bot(client, "POST")
    assert chat(client, json={"body": "Gopnik: here again"}).json() == {"sent": False, "banned": True}
    assert chat(client, json={"body": "Tolik: not me"}).json() == {"sent": True}
    assert said == ["Tolik"]


def test_a_hall_ban_holds_when_the_display_name_differs_from_the_login(client, gate, monkeypatch):
    said = []
    monkeypatch.setattr(gate.twins, "say", lambda name, text: asyncio.sleep(0, said.append(name)))
    gate.state.bans["id:7"] = {"login": "vasya", "name": "vasya", "oc_ids": ["x"], "at": 0}
    bot(client, "POST")
    # the bot sends the display name in the line and the login beside it
    assert chat(client, json={"body": "Вася: here again", "login": "vasya"}).json() == {"sent": False, "banned": True}
    assert chat(client, json={"body": "Вася: no login"}).json() == {"sent": True}  # an older bot sends none
    assert said == ["Вася"]


def test_a_twin_ban_outlives_the_chat_history(gate, monkeypatch):
    history = [{"user": {"id": "t1", "displayName": "troll [TW]", "disabledAt": "2026-01-01T00:00:00Z"}}]

    async def owncast(method, url, **kw):
        if url.endswith("/api/admin/chat/users/disabled"):
            return 200, []
        if url.endswith("/api/admin/chat/messages"):
            return 200, history
        raise AssertionError(url)

    monkeypatch.setattr(gate.upstream, "http", owncast)
    asyncio.run(gate.moderation.sync_bans())
    assert gate.state.bans["tw:troll"]["oc_ids"] == ["t1"]
    history.clear()  # the twin's lines left the history, so Owncast no longer shows it
    asyncio.run(gate.moderation.sync_bans())
    assert "tw:troll" in gate.state.bans
    history.append({"user": {"id": "t1", "displayName": "troll [TW]"}})  # seen again and enabled in the admin
    asyncio.run(gate.moderation.sync_bans())
    assert "tw:troll" not in gate.state.bans


@needs_owncast
def test_a_viewer_banned_in_the_chat_is_out_of_the_hall_until_unbanned(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert client.get("/_gate/auth").status_code == 200
    oc_id = gate.state.chatters["7"]["oc_id"]
    admin("/api/admin/chat/users/setenabled", {"userId": oc_id, "enabled": False})
    try:
        asyncio.run(gate.moderation.sync_bans())
        assert gate.state.bans["id:7"]["login"] == "gopnik"
        assert json.loads(gate.config.BANS_FILE.read_text())["id:7"]["oc_ids"] == [oc_id]  # permanent, not per session
        assert client.get("/_gate/auth").status_code == 302  # dropped from the video at once
        assert twitch_step(client).status_code == 403  # and cannot log in again
    finally:
        admin("/api/admin/chat/users/setenabled", {"userId": oc_id, "enabled": True})
    asyncio.run(gate.moderation.sync_bans())
    assert "id:7" not in gate.state.bans  # re-enabled in Owncast, so the ban is lifted


@needs_owncast
def test_a_twitch_viewer_banned_in_the_hall_gets_no_fallback_line(client, gate):
    bot(client, "POST")
    mark = f"first {os.getpid()}-{time.time()}"  # Owncast holds many «Banme [TW]» twins left by other test runs
    assert chat(client, json={"body": f"Banme: {mark}"}).json() == {"sent": True}
    twin_id = next(m["user"]["id"] for m in by(gate, "Banme [TW]") if mark in m["body"])
    admin("/api/admin/chat/users/setenabled", {"userId": twin_id, "enabled": False})
    try:
        after = f"after the ban {time.time()}"
        assert chat(client, json={"body": f"Banme: {after}"}).json() == {"sent": False, "banned": True}
        assert not any(after in m["body"] for m in hall_history(gate))  # and none from «Twitch» either
        asyncio.run(gate.moderation.sync_bans())
        assert gate.state.bans["tw:banme"]["oc_ids"] == [twin_id]
    finally:
        admin("/api/admin/chat/users/setenabled", {"userId": twin_id, "enabled": True})
    asyncio.run(gate.moderation.sync_bans())
    assert "tw:banme" not in gate.state.bans  # unbanned in Owncast, so lines go through again
    assert chat(client, json={"body": "Banme: allowed again"}).json() == {"sent": True}


def test_only_the_streamer_sees_and_lifts_bans(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "OWNCAST_URL", "http://owncast.invalid")
    gate.state.bans["tw:banme"] = {"login": "banme", "name": "banme [TW]", "oc_ids": ["x"], "at": 5}
    gate.state.bans["id:7"] = {"login": "gopnik", "name": "Gopnik", "oc_ids": ["y"], "at": 9}
    bot(client, "POST")
    assert client.get("/_gate/bans").status_code == 403
    assert client.post("/_gate/unban", json={"key": "id:7"}).status_code == 403
    client.cookies.set("kzh", gate.security.HOST_KEY)  # the permanent dock pass means the streamer
    r = client.get("/_gate/bans")  # Owncast is down, so the list comes from the file
    assert r.status_code == 200
    assert r.json() == {
        "bans": [
            {"key": "id:7", "name": "Gopnik", "twitch": False, "at": 9},
            {"key": "tw:banme", "name": "banme", "twitch": True, "at": 5},
        ]
    }
    assert client.post("/_gate/unban", json={"key": "id:404"}).status_code == 404
    assert client.post("/_gate/unban", json={"key": "id:7"}).status_code == 502  # Owncast is silent, so the ban stays
    assert "id:7" in gate.state.bans


@needs_owncast
def test_unban_from_the_hall_lets_the_viewer_back_and_lifts_the_ip_ban(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    viewer = client.cookies.get("kz")
    record = gate.state.chatters["7"]
    with connect(f"{OWNCAST.replace('http', 'ws')}/ws?accessToken={record['oc_token']}"):
        time.sleep(0.5)
        admin("/api/admin/chat/users/setenabled", {"userId": record["oc_id"], "enabled": False})
        time.sleep(0.5)
    try:
        ip_bans = [b for b in admin("/api/admin/chat/users/ipbans") if b["notes"] == f"Banning of {record['name']}"]
        assert ip_bans, "Owncast banned no IP, so the test cannot check it"
        client.cookies.set("kzh", gate.security.HOST_KEY)
        listed = client.get("/_gate/bans").json()["bans"]
        row = {k: listed[0][k] for k in ("key", "name", "twitch")}
        assert row == {"key": "id:7", "name": record["name"], "twitch": False}
        assert client.post("/_gate/unban", json={"key": "id:7"}).status_code == 204
        assert "id:7" not in gate.state.bans and "id:7" not in json.loads(gate.config.BANS_FILE.read_text())
        assert not [b for b in admin("/api/admin/chat/users/ipbans") if b["notes"] == f"Banning of {record['name']}"]
        assert record["oc_id"] not in {u["id"] for u in admin("/api/admin/chat/users/disabled")}
        asyncio.run(gate.moderation.sync_bans())
        assert "id:7" not in gate.state.bans  # the sync does not restore a lifted ban
        client.cookies.delete("kzh")
        client.cookies.set("kz", viewer)
        assert client.get("/_gate/auth").status_code == 200  # watching again
    finally:
        admin("/api/admin/chat/users/setenabled", {"userId": record["oc_id"], "enabled": True})
        for b in admin("/api/admin/chat/users/ipbans"):
            if b["notes"] == f"Banning of {record['name']}":
                admin("/api/admin/chat/users/ipbans/remove", {"value": b["ipAddress"]})


@needs_owncast
def test_the_streamer_is_the_chat_moderator_and_the_dock_writes_as_the_streamer(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.user = {"id": CHANNEL_ID, "login": "exitfound", "display_name": "exitfound"}
    login(client)
    oc_id = gate.state.chatters[CHANNEL_ID]["oc_id"]
    assert oc_id in [u["id"] for u in admin("/api/admin/chat/users/moderators")]
    assert gate.state.look["badges"]["exitfound"] == ["broadcaster/1"]
    r = client.get(f"/_gate/host?key={gate.security.HOST_KEY}&next=/embed/chat/readwrite/")
    assert handoff_token(r) == gate.state.chatters[CHANNEL_ID]["oc_token"]


@needs_owncast
def test_a_viewer_is_not_a_moderator(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert gate.state.chatters["7"]["oc_id"] not in [u["id"] for u in admin("/api/admin/chat/users/moderators") or []]


def test_the_streamer_signed_in_with_twitch_manages_bans_and_a_forged_cookie_does_not(client, gate, monkeypatch):
    """streamer_ok() takes a Twitch sign-in as the channel only from a signed entry cookie."""
    monkeypatch.setattr(gate.config, "OWNCAST_URL", "http://owncast.invalid")  # the list then comes from the file
    bot(client, "POST")
    gate.state.chatters[CHANNEL_ID] = {"login": "exitfound", "name": "exitfound", "oc_id": "oc-s", "oc_token": "s"}
    gate.state.chatters["7"] = {"login": "gopnik", "name": "Gopnik", "oc_id": "oc-7", "oc_token": "g"}
    sid = gate.state.session["sid"]

    def bans_as(cookie: str) -> int:
        client.cookies.clear()
        client.cookies.set("kz", cookie)
        return client.get("/_gate/bans").status_code

    assert bans_as(gate.access.cookie_value(CHANNEL_ID)) == 200
    assert bans_as(gate.access.cookie_value("7")) == 403
    assert bans_as(f"{sid}.{gate.security.sign(sid)}.{CHANNEL_ID}.forged") == 403


def test_bans_are_kept_in_their_file_as_owncast_changes(gate, monkeypatch):
    """Every new ban, every new chat user of a banned account and every lifted ban reaches /data/bans.json."""
    disabled = [{"id": "oc-7"}]  # Owncast may leave out the nick
    gate.state.chatters["7"] = {"login": "Gopnik", "name": "Gopnik", "oc_id": "oc-7", "oc_token": "t"}

    async def owncast(method, url, **kw):
        if url.endswith("/api/admin/chat/users/disabled"):
            return 200, disabled
        if url.endswith("/api/admin/chat/messages"):
            return 200, []
        raise AssertionError(url)

    def saved() -> dict:
        return json.loads(gate.config.BANS_FILE.read_text())

    monkeypatch.setattr(gate.upstream, "http", owncast)
    asyncio.run(gate.moderation.sync_bans())
    assert saved()["id:7"]["oc_ids"] == ["oc-7"] and saved()["id:7"]["login"] == "gopnik"
    assert saved()["id:7"]["name"] == ""
    gate.state.chatters["7"]["oc_id"] = "oc-7b"  # the same account in a new session: a new chat user, banned too
    disabled.append({"id": "oc-7b", "displayName": "Gopnik"})
    asyncio.run(gate.moderation.sync_bans())
    assert saved()["id:7"]["oc_ids"] == ["oc-7", "oc-7b"]
    disabled.clear()
    asyncio.run(gate.moderation.sync_bans())
    assert saved() == {}


def test_a_sync_that_changes_nothing_writes_nothing(gate, monkeypatch):
    async def owncast(method, url, **kw):
        return 200, []

    writes = []
    monkeypatch.setattr(gate.upstream, "http", owncast)
    monkeypatch.setattr(gate.state, "save_bans", lambda: writes.append(True))
    asyncio.run(gate.moderation.sync_bans())
    assert writes == []


def test_only_twins_count_as_twins_in_the_chat_history(gate, monkeypatch):
    """A banned hall user without the twin mark is no Twitch ban, and a history entry without an id is skipped."""
    history = [
        {"user": {"id": "a1", "displayName": "anon", "disabledAt": "2026-01-01T00:00:00Z"}},
        {"user": {"displayName": "ghost [TW]", "disabledAt": "2026-01-01T00:00:00Z"}},
        {"user": {"id": "t2", "displayName": "troublemaker [TW]", "disabledAt": "2026-01-01T00:00:00Z"}},
    ]

    async def owncast(method, url, **kw):
        if url.endswith("/api/admin/chat/users/disabled"):
            return 200, [{"id": "a1", "displayName": "anon"}]
        return 200, history

    monkeypatch.setattr(gate.upstream, "http", owncast)
    asyncio.run(gate.moderation.sync_bans())
    assert set(gate.state.bans) == {"tw:troublemaker"}


@pytest.mark.parametrize("path", ["/api/admin/chat/users/disabled", "/api/admin/chat/messages"])
def test_a_malformed_owncast_answer_fails_the_ban_sync(gate, monkeypatch, path):
    """An answer that is not a list is an Upstream failure: the hall loop logs it and the bans stay as they are."""

    async def owncast(method, url, **kw):
        return 200, {"error": "not a list"} if url.endswith(path) else []

    monkeypatch.setattr(gate.upstream, "http", owncast)
    with pytest.raises(gate.upstream.Upstream):
        asyncio.run(gate.moderation.sync_bans())


def unban_with(gate, monkeypatch, answers: dict) -> list:
    """Owncast answers each admin path from answers; returns the (path, body) of every call."""
    calls = []

    async def owncast(method, url, json_body=None, **kw):
        path = url.removeprefix(gate.config.OWNCAST_URL)
        calls.append((path, json_body))
        return answers[path]

    monkeypatch.setattr(gate.upstream, "http", owncast)
    gate.state.bans["id:7"] = {"login": "gopnik", "name": "Gopnik", "oc_ids": ["oc7", "oc7b"], "at": 1}
    return calls


def test_unban_enables_every_chat_user_and_lifts_only_their_ip_bans(gate, monkeypatch):
    ips = [
        {"notes": "BANNING OF GOPNIK", "ipAddress": "1.1.1.1"},
        {"notes": "Banning of gopnik", "ipAddress": "2.2.2.2"},
        {"notes": "Banning of Tolik", "ipAddress": "3.3.3.3"},
        {"notes": "Banning of Gopnik"},
        "junk",
    ]
    calls = unban_with(
        gate,
        monkeypatch,
        {
            "/api/admin/chat/users/setenabled": (200, None),
            "/api/admin/chat/users/ipbans": (200, ips),
            "/api/admin/chat/users/ipbans/remove": (200, None),
        },
    )
    assert asyncio.run(gate.moderation.unban("id:7")) is True
    assert [body for path, body in calls if path.endswith("setenabled")] == [
        {"userId": "oc7", "enabled": True},
        {"userId": "oc7b", "enabled": True},
    ]
    assert [body["value"] for path, body in calls if path.endswith("remove")] == ["1.1.1.1", "2.2.2.2"]
    assert gate.state.bans == {} and json.loads(gate.config.BANS_FILE.read_text()) == {}
    assert asyncio.run(gate.moderation.unban("id:7")) is False


@pytest.mark.parametrize(
    "failing",
    [
        {"/api/admin/chat/users/setenabled": (500, None)},
        {"/api/admin/chat/users/ipbans": (200, {"error": "no list"})},
        {"/api/admin/chat/users/ipbans": (500, [])},
        {"/api/admin/chat/users/ipbans/remove": (500, None)},
    ],
)
def test_an_owncast_failure_during_unban_keeps_the_ban(gate, monkeypatch, failing):
    answers = {
        "/api/admin/chat/users/setenabled": (200, None),
        "/api/admin/chat/users/ipbans": (200, [{"notes": "Banning of Gopnik", "ipAddress": "1.1.1.1"}]),
        "/api/admin/chat/users/ipbans/remove": (200, None),
    }
    unban_with(gate, monkeypatch, answers | failing)
    with pytest.raises(gate.upstream.Upstream, match="unban of gopnik"):
        asyncio.run(gate.moderation.unban("id:7"))
    assert "id:7" in gate.state.bans
