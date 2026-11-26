"""The chat look: Twitch nick colours, emotes and badges."""

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from helpers import (
    bot,
    chat,
    login,
    needs_owncast,
)


def test_the_look_is_only_for_those_inside(client):
    assert client.get("/_gate/look").status_code == 401


def test_a_twitch_line_brings_its_author_s_color_and_emotes(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate.twins, "say", lambda name, text: asyncio.sleep(0))
    bot(client, "POST")
    assert chat(
        client,
        json={
            "body": "Gopnik: hello Kappa sub7Hype",
            "color": "#ff0000",
            "emotes": {"Kappa": "25", "sub7Hype": "emotesv2_ff00"},
        },
    ).json() == {"sent": True}
    # Malformed parts are dropped silently. Here a bad colour, an id or a name with a space, a non-string id
    chat(client, json={"body": "Tolik: hello", "color": "red", "emotes": {"Bad": "1 2", "a b": "3", "Ok": 5}})
    assert gate.state.look["colors"] == {"gopnik": "#FF0000"}
    assert gate.state.look["emotes"] == {"Kappa": "25", "sub7Hype": "emotesv2_ff00"}
    assert json.loads(gate.config.LOOK_FILE.read_text())["colors"] == {"gopnik": "#FF0000"}  # survives a restart


def test_inside_the_look_has_colors_and_twitch_and_channel_emotes(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", True)
    gate.look.remember("Gopnik", "#00ff7f", {"sub7Hype": "emotesv2_ff00"})
    look = client.get("/_gate/look").json()
    assert look["colors"] == {"gopnik": "#00FF7F"}
    assert look["emotes"] == {"Kappa": "25", "exitfoLove": "emotesv2_abc123", "sub7Hype": "emotesv2_ff00"}
    assert "{id}" in look["url"]
    calls = len(fake_twitch.calls)
    client.get("/_gate/look")
    assert len(fake_twitch.calls) == calls  # the second request takes Twitch emotes from the cache


def test_without_twitch_emotes_the_look_still_answers(client, gate, fake_twitch, monkeypatch):
    fake_twitch.emotes_status = 500
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", True)
    assert client.get("/_gate/look").json()["emotes"] == {}


@needs_owncast
def test_signing_in_with_twitch_takes_the_viewer_s_chat_color(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert gate.state.look["colors"] == {"gopnik": "#FF4500"}


@needs_owncast
def test_a_viewer_without_a_chosen_color_keeps_the_hall_one(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows, fake_twitch.color = True, ""
    login(client)
    assert gate.state.look["colors"] == {}


def test_badges_come_with_twitch_lines_and_only_used_images_go_out(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate.twins, "say", lambda name, text: asyncio.sleep(0))
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", True)
    chat(client, json={"body": "Gopnik: hello", "badges": ["moderator/1", "subscriber/0", "кривой", 5]})
    look = client.get("/_gate/look").json()
    assert look["badges"] == {"gopnik": ["moderator/1", "subscriber/0"]}
    # the channel's badge wins over the global one
    assert look["badgeImages"] == {
        "moderator/1": ["https://b/mod1", "https://b/mod2", "Moderator"],
        "subscriber/0": ["https://b/csub", "https://b/csub2", "Subscriber"],
    }
    chat(client, json={"body": "Gopnik: badges off", "badges": []})
    assert gate.state.look["badges"] == {}


def inside(client, gate, monkeypatch) -> None:
    """Enter by the code alone, then switch the Twitch app back on: the look's Twitch side runs, no sign-in needed."""
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", True)


def answering(gate, monkeypatch, path: str, answer) -> None:
    """Twitch answers this Helix path with this (status, body); the rest goes to fake_twitch."""
    real = gate.upstream.http_sync

    def patched(method, url, **kw):
        return answer if urlparse(url).path == path else real(method, url, **kw)

    monkeypatch.setattr(gate.upstream, "http_sync", patched)


def test_a_change_of_badges_or_colour_alone_reaches_the_file(gate):
    def saved() -> dict:
        return json.loads(gate.config.LOOK_FILE.read_text())

    gate.look.remember("Gopnik", None, None, ["moderator/1"])
    assert saved()["badges"] == {"gopnik": ["moderator/1"]}
    gate.look.remember("Gopnik", "#00ff00", None)
    assert saved()["colors"] == {"gopnik": "#00FF00"}


def test_a_repeated_line_rewrites_nothing(gate, monkeypatch):
    writes = []
    monkeypatch.setattr(gate.state, "save_look", lambda: writes.append(True))
    for _ in range(3):
        gate.look.remember("Gopnik", "#00ff00", {"Kappa": "25"}, ["moderator/1"])
    assert len(writes) == 1


def test_the_look_keeps_the_most_recent_nicks_within_its_cap(gate, monkeypatch):
    monkeypatch.setattr(gate.look, "COLORS_MAX", 2)
    for nick in ("anna", "boris", "vera"):
        gate.look.remember(nick, "#00ff00", None)
    assert list(gate.state.look["colors"]) == ["boris", "vera"]


def test_a_failed_emote_fetch_is_retried_after_ten_minutes_not_on_every_request(client, gate, fake_twitch, monkeypatch):
    inside(client, gate, monkeypatch)
    clock = [time.time()]
    monkeypatch.setattr(gate.look, "time", SimpleNamespace(time=lambda: clock[0]))
    fake_twitch.emotes_status = 500

    def fetches() -> int:
        client.get("/_gate/look")
        return fake_twitch.calls.count("/helix/chat/emotes/global")

    assert fetches() == 1
    clock[0] += gate.look.TWITCH_EMOTES_RETRY - 1
    assert fetches() == 1
    clock[0] += 2
    assert fetches() == 2


def test_only_https_badge_images_go_out(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate.twins, "say", lambda name, text: asyncio.sleep(0))
    inside(client, gate, monkeypatch)
    plain = {"set_id": "glitch", "versions": [{"id": "1", "image_url_1x": "http://b/plain"}]}
    answering(gate, monkeypatch, "/helix/chat/badges/global", (200, {"data": [plain]}))
    chat(client, json={"body": "Gopnik: hello", "badges": ["glitch/1"]})
    assert client.get("/_gate/look").json()["badgeImages"] == {}


@pytest.mark.parametrize("path", ["/helix/chat/emotes/global", "/helix/chat/badges"])
@pytest.mark.parametrize("answer", [(200, {"data": None}), (200, None)])
def test_a_malformed_helix_list_leaves_the_emotes_empty(client, gate, fake_twitch, monkeypatch, capsys, path, answer):
    inside(client, gate, monkeypatch)
    answering(gate, monkeypatch, path, answer)
    assert client.get("/_gate/look").json()["emotes"] == {}
    assert f"Twitch emotes and badges: {path.removeprefix('/helix/')}: HTTP 200" in capsys.readouterr().out


@pytest.mark.parametrize("answer", [(200, {}), (200, None), (500, {"access_token": "app-token"})])
def test_an_app_token_answer_without_a_usable_token_leaves_the_emotes_empty(
    client, gate, fake_twitch, monkeypatch, answer
):
    inside(client, gate, monkeypatch)
    answering(gate, monkeypatch, "/oauth2/token", answer)
    assert client.get("/_gate/look").json()["emotes"] == {}


def test_the_app_token_is_reused_until_five_minutes_before_it_expires(gate, monkeypatch):
    clock, issued = [1000.0], []

    def token(method, url, **kw):
        issued.append(clock[0])
        return 200, {"access_token": f"app-{len(issued)}", "expires_in": 3600}

    monkeypatch.setattr(gate.upstream, "http_sync", token)
    monkeypatch.setattr(gate.twitch, "time", SimpleNamespace(time=lambda: clock[0]))
    assert asyncio.run(gate.twitch.app_access_token()) == "app-1"
    clock[0] += 3600 - 300 - 1
    assert asyncio.run(gate.twitch.app_access_token()) == "app-1"
    clock[0] += 2
    assert asyncio.run(gate.twitch.app_access_token()) == "app-2"


@pytest.mark.parametrize(
    "answer, color",
    [
        ((200, {"data": [{"color": "#FF4500"}]}), "#FF4500"),
        ((200, {"data": [{"color": ""}]}), None),
        ((200, {"data": [{"color": "red"}]}), None),
        ((200, {"data": []}), None),
        ((200, None), None),
        ((500, {"data": [{"color": "#FF4500"}]}), None),
    ],
)
def test_signing_in_takes_the_viewer_s_twitch_colour_when_it_has_one(
    client, gate, fake_twitch, fake_chat, monkeypatch, answer, color
):
    bot(client, "POST")
    fake_twitch.follows = True
    answering(gate, monkeypatch, "/helix/chat/color", answer)
    assert login(client).status_code == 200
    assert gate.state.look["colors"] == ({"gopnik": color} if color else {})


def test_an_app_token_without_an_expiry_counts_for_an_hour(gate, monkeypatch):
    clock, issued = [1000.0], []

    def token(method, url, **kw):
        issued.append(clock[0])
        return 200, {"access_token": f"app-{len(issued)}"}

    monkeypatch.setattr(gate.upstream, "http_sync", token)
    monkeypatch.setattr(gate.twitch, "time", SimpleNamespace(time=lambda: clock[0]))
    asyncio.run(gate.twitch.app_access_token())
    clock[0] += 3600 - 301
    assert asyncio.run(gate.twitch.app_access_token()) == "app-1"
