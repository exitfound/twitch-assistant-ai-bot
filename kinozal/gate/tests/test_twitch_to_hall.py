"""Twitch lines into the hall: the bot's /_gate/api/chat, the twins «nick [TW]» and the «Twitch» fallback."""

import asyncio
import json
import urllib.request

import pytest

from helpers import (
    OWNCAST,
    bot,
    by,
    chat,
    needs_owncast,
)


def test_the_chat_bridge_needs_the_bot_token(client):
    assert client.post("/_gate/api/chat").status_code == 401
    assert client.post("/_gate/api/chat", headers={"Authorization": "Bearer nope"}).status_code == 401


@needs_owncast
def test_twitch_lines_reach_the_hall_from_the_author_s_twin(client, gate):
    bot(client, "POST")
    assert chat(client, "POST", json={"body": "Gopnik: **hello** from twitch @dv4mp1r3"}).json() == {"sent": True}
    assert chat(client, "POST", json={"user": "Gopnik", "body": "second"}).json() == {"sent": True}
    lines = by(gate, "Gopnik [TW]")[-2:]  # Owncast is shared by all tests
    assert lines[0]["user"]["isBot"]
    assert "**hello**" in lines[0]["body"] and "<strong>@dv4mp1r3</strong>" in lines[0]["body"]
    assert "Gopnik:" not in lines[0]["body"] and "second" in lines[1]["body"]
    assert list(gate.state.twins) == ["gopnik"]  # one twin per viewer


@needs_owncast
def test_a_twitch_viewer_s_twin_does_not_take_the_hall_name(client, gate):
    bot(client, "POST")
    chat(client, "POST", json={"body": "Tolik: hello"})
    reg = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{OWNCAST}/api/chat/register",
                method="POST",
                data=b'{"displayName":"Tolik"}',
                headers={"Content-Type": "application/json"},
            )
        ).read()
    )
    assert reg["displayName"] == "Tolik"


@needs_owncast
def test_without_a_twin_the_line_goes_from_twitch(client, gate, monkeypatch):
    bot(client, "POST")

    async def broken(name, fresh=False):
        raise gate.upstream.Upstream("nope")

    monkeypatch.setattr(gate.twins, "token", broken)
    assert chat(client, "POST", json={"body": "gopnik_1: **hello**"}).json() == {"sent": True}
    assert "gopnik_1: **hello**" in by(gate, "Twitch")[-1]["body"]


def test_a_peer_dropping_the_answer_is_an_upstream_failure(gate, monkeypatch):
    """Owncast restarting mid-answer raises http.client errors. They must surface as Upstream."""
    from http.client import IncompleteRead

    def cut(*args, **kwargs):
        raise IncompleteRead(b"[")

    monkeypatch.setattr(urllib.request, "urlopen", cut)
    with pytest.raises(gate.upstream.Upstream):
        gate.upstream.http_sync("GET", "http://owncast.invalid/api/admin/chat/users/disabled")


def test_an_unexplained_401_does_not_fall_back_to_the_shared_sender(client, gate, monkeypatch):
    """Owncast refused the twin and the ban check failed. The author may be banned, so the line is dropped."""
    bot(client, "POST")
    sent = []

    async def token(name, fresh=False):
        return "twin-token"

    async def owncast(method, url, **kw):
        if url.endswith("/api/integrations/chat/send"):
            sent.append(kw["headers"]["Authorization"])
            return 401, None
        raise gate.upstream.Upstream("admin chat history timed out")

    async def shared(*args, **kwargs):
        raise AssertionError("the line went out through the shared «Twitch» user")

    monkeypatch.setattr(gate.twins, "token", token)
    monkeypatch.setattr(gate.upstream, "http", owncast)
    monkeypatch.setattr(gate.owncast, "integration", shared)
    r = chat(client, json={"body": "troll: hello"})
    assert r.status_code == 502 and sent == ["Bearer twin-token"]


@needs_owncast
def test_a_twin_line_that_starts_like_a_block_reaches_the_hall_whole(client, gate):
    bot(client, "POST")
    for body in ("+", "- item", "1. yes"):
        assert chat(client, "POST", json={"user": "Plusman", "body": body}).json() == {"sent": True}
    lines = [m["body"] for m in by(gate, "Plusman [TW]")[-3:]]  # from the twin, not the «Twitch» fallback
    assert lines == ["<p>+</p>", "<p>- item</p>", "<p>1. yes</p>"]


@needs_owncast
def test_a_revoked_chat_token_is_taken_again(client, gate):
    gate.owncast.chat_token = "revoked"
    asyncio.run(gate.owncast.integration("POST", "/api/integrations/chat/send", {"body": "check"}))
    assert gate.owncast.chat_token != "revoked"


@needs_owncast
def test_a_revoked_twin_token_is_taken_again(client, gate):
    gate.state.twins["gopnik"] = "revoked"
    assert chat(client, json={"body": "Gopnik: check"}).json() == {"sent": True}
    assert gate.state.twins["gopnik"] != "revoked"


def test_an_empty_line_is_refused(client):
    assert chat(client, "POST", json={"body": "  "}).status_code == 400
    assert chat(client, "POST", content=b"not json").status_code == 400


def test_the_author_comes_from_user_or_from_the_line(client, gate, monkeypatch):
    """{"user"} names the author and keeps the body whole; without it, a leading «nick: » does."""
    said = []

    async def twin(name, text):
        said.append((name, text))

    monkeypatch.setattr(gate.twins, "say", twin)
    bot(client, "POST")
    chat(client, json={"user": "Gopnik", "body": "note: this stays"})
    chat(client, json={"body": "Tolik: hello"})
    chat(client, json={"user": "  ", "body": "Vasya: hi"})
    assert said == [("Gopnik", "note: this stays"), ("Tolik", "hello"), ("Vasya", "hi")]


def test_a_login_that_is_not_banned_lets_the_line_through(client, gate, monkeypatch):
    monkeypatch.setattr(gate.twins, "say", lambda name, text: asyncio.sleep(0))
    gate.state.bans["id:9"] = {"login": "troll", "name": "troll", "oc_ids": ["x"], "at": 0}
    bot(client, "POST")
    assert chat(client, json={"body": "Tolik: hello", "login": "tolik"}).json() == {"sent": True}


def test_the_shared_twitch_token_is_cached_until_a_fresh_one_is_asked(gate, monkeypatch):
    made = []

    async def token(name, scopes):
        made.append(name)
        return f"token-{len(made)}"

    monkeypatch.setattr(gate.owncast, "integration_token", token)
    assert asyncio.run(gate.owncast.get_chat_token()) == "token-1"
    assert asyncio.run(gate.owncast.get_chat_token()) == "token-1"
    assert asyncio.run(gate.owncast.get_chat_token(fresh=True)) == "token-2"
    assert made == [gate.owncast.CHAT_BOT_NAME] * 2
