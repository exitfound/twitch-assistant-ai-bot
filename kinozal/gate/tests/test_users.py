"""Who is in the hall chat: the member list and the «@» hint."""

from types import SimpleNamespace

from helpers import bot, needs_owncast


def test_the_user_list_is_only_for_those_inside(client):
    assert client.get("/_gate/users").status_code == 401


def test_chat_users_are_one_per_person_without_bots_and_with_moderators(gate):
    clients = [
        {"user": {"id": "a", "displayName": "Gopnik", "scopes": ["MODERATOR"]}},
        {"user": {"id": "a", "displayName": "Gopnik"}},  # second tab
        {"user": {"id": "b", "displayName": "anna"}},
        {"user": {"id": "d", "displayName": "anna"}},  # another person with the same hall nick
        {"user": {"id": "c", "displayName": "Twitch", "isBot": True}},  # integration
        {"user": None},
        "junk",
    ]
    assert gate.owncast.chat_users(clients) == [
        {"name": "anna", "mod": False},
        {"name": "anna", "mod": False},
        {"name": "Gopnik", "mod": True},
    ]
    assert gate.owncast.chat_users(None) == []


@needs_owncast
def test_inside_the_user_list_comes_from_owncast_and_is_cached(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    calls = []
    real = gate.upstream.http_sync
    monkeypatch.setattr(gate.upstream, "http_sync", lambda *a, **kw: calls.append(a[1]) or real(*a, **kw))
    first = client.get("/_gate/users")
    assert first.status_code == 200 and isinstance(first.json()["users"], list)
    client.get("/_gate/users")
    assert sum("/chat/clients" in url for url in calls) == 1  # second request from the cache


def test_without_owncast_the_user_list_says_so_and_does_not_retry_at_once(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    calls = []

    def down(*a, **kw):
        calls.append(a[1])
        raise gate.upstream.Upstream("no connection")

    monkeypatch.setattr(gate.upstream, "http_sync", down)
    assert client.get("/_gate/users").status_code == 502
    assert client.get("/_gate/users").status_code == 502
    assert len(calls) == 1  # a failure is cached too, so the second request does not wait for Owncast


def test_without_owncast_the_user_list_asks_again_once_its_pause_is_over(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    clock, calls = [1000.0], []
    monkeypatch.setattr(gate.routers.hall, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def down(*a, **kw):
        calls.append(a[1])
        raise gate.upstream.Upstream("no connection")

    monkeypatch.setattr(gate.upstream, "http_sync", down)
    client.get("/_gate/users")
    clock[0] += gate.routers.hall.USERS_TTL + 1
    assert client.get("/_gate/users").status_code == 502
    assert len(calls) == 2
