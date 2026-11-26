"""The bot's session API: its refusals and the shape of its answers, a contract with the running bot."""

import pytest

from helpers import bot, chat


@pytest.mark.parametrize("method", ["POST", "PATCH", "GET", "DELETE"])
def test_the_session_api_needs_the_bot_token(client, method):
    assert client.request(method, "/_gate/api/session").status_code == 401
    assert client.request(method, "/_gate/api/session", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert bot(client, "GET").json() == {"active": False}


def test_an_open_session_answers_with_its_code_start_and_title(client):
    opened = bot(client, "POST", json={"title": "Alien"}).json()
    assert set(opened) == {"active", "code", "started", "title"}
    assert opened["active"] is True and opened["title"] == "Alien"
    assert len(opened["code"]) == 4 and opened["code"].isdigit()
    assert bot(client, "GET").json() == opened


def test_retitling_without_a_session_says_there_is_none(client):
    r = bot(client, "PATCH", json={"title": "Solaris"})
    assert r.status_code == 409 and r.json() == {"active": False}


@pytest.mark.parametrize("body", [b"not json", b"[1]", b'{"title": 5}'])
def test_a_malformed_title_is_refused_and_changes_nothing(client, body):
    r = bot(client, "POST", content=body)
    assert r.status_code == 400 and r.json()["error"]
    assert bot(client, "GET").json() == {"active": False}
    bot(client, "POST", json={"title": "Alien"})
    assert bot(client, "PATCH", content=body).status_code == 400
    assert bot(client, "GET").json()["title"] == "Alien"


def test_a_title_is_one_line_of_at_most_200_characters(client):
    assert bot(client, "POST", json={"title": "  Alien \n  Romulus  "}).json()["title"] == "Alien Romulus"
    assert bot(client, "PATCH", json={"title": "x" * 300}).json()["title"] == "x" * 200
    assert bot(client, "PATCH", json={"title": "   "}).json()["title"] is None
    assert bot(client, "PATCH", json={}).json()["title"] is None
    assert bot(client, "PATCH").json()["title"] is None


def test_the_health_check_answers_without_a_token(client):
    assert client.get("/_gate/health").json() == {"ok": True}


def test_a_twitch_line_owncast_refuses_is_a_502(client, gate, monkeypatch):
    """No twin and no shared «Twitch» sender: the bot learns that the line did not reach the hall."""

    async def down(*args, **kwargs):
        raise gate.upstream.Upstream("owncast down")

    monkeypatch.setattr(gate.twins, "token", down)
    monkeypatch.setattr(gate.owncast, "integration", down)
    bot(client, "POST")
    r = chat(client, json={"body": "Gopnik: hello"})
    assert r.status_code == 502 and "owncast down" in r.json()["error"]
