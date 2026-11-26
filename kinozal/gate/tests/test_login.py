"""The Twitch sign-in: who gets in, the chat user it gives, and the code still asked after it."""

import base64
import json
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from helpers import (
    CHANNEL_ID,
    OWNCAST,
    OWNCAST_PASSWORD,
    bot,
    can_watch,
    handoff,
    handoff_token,
    login,
    names,
    needs_owncast,
    twitch_step,
)


def test_without_a_session_twitch_login_shows_closed(client, fake_twitch):
    r = client.get("/_gate/twitch")
    assert r.status_code == 200 and "Сейчас сеанса нет" in r.text
    assert fake_twitch.calls == []


def test_a_forged_state_is_refused(client, fake_twitch):
    bot(client, "POST")
    client.get("/_gate/twitch")
    r = client.get("/_gate/twitch/callback?code=good-code&state=forged")
    assert r.status_code == 400 and not can_watch(client)
    assert fake_twitch.calls == []


def test_a_callback_without_the_cookie_is_refused(client, fake_twitch):
    bot(client, "POST")
    r = client.get("/_gate/twitch/callback?code=good-code&state=whatever")
    assert r.status_code == 400 and not can_watch(client)


def test_cancel_on_the_twitch_screen_offers_the_code(client, fake_twitch):
    bot(client, "POST")
    client.get("/_gate/twitch")
    r = client.get("/_gate/twitch/callback?error=access_denied&state=x")
    assert "отменён" in r.text and not can_watch(client)


def test_a_non_follower_is_refused_and_the_token_revoked(client, fake_twitch):
    bot(client, "POST")
    r = login(client)
    assert r.status_code == 403 and "фолловеров и сабов" in r.text
    assert not can_watch(client)
    assert fake_twitch.revoked == ["user-token"]


def test_twitch_down_is_said_and_the_token_still_revoked(client, fake_twitch, gate, monkeypatch):
    bot(client, "POST")
    real = gate.upstream.http_sync

    def down(method, url, **kw):
        if "channels/followed" in url:
            raise gate.upstream.Upstream("timeout")
        return real(method, url, **kw)

    monkeypatch.setattr(gate.upstream, "http_sync", down)
    r = login(client)
    assert r.status_code == 502 and not can_watch(client)
    assert fake_twitch.revoked == ["user-token"]


def test_a_bad_code_exchange_is_refused(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.token_status = 400
    assert login(client).status_code == 400 and not can_watch(client)


@needs_owncast
@pytest.mark.parametrize("who", ["follower", "sub", "broadcaster"])
def test_followers_subs_and_the_streamer_get_in_with_their_chat_name(client, fake_twitch, gate, who):
    bot(client, "POST")
    if who == "follower":
        fake_twitch.follows = True
    elif who == "sub":
        fake_twitch.sub = True
    else:
        fake_twitch.user = {"id": CHANNEL_ID, "login": "exitfound", "display_name": "ExitFound"}
    r = login(client, nxt="/embed/chat/readwrite/")
    assert r.status_code == 200, r.text
    assert handoff(r.text)["next"] == "/embed/chat/readwrite/"
    assert can_watch(client)
    assert fake_twitch.revoked == []  # kept, since hall lines go to Twitch with it
    # The page's token belongs to a real Owncast chat user with the Twitch nick
    token = handoff_token(r)
    with urllib.request.urlopen(f"{OWNCAST}/api/chat?accessToken={token}") as resp:
        assert resp.status == 200
    assert fake_twitch.user["display_name"] in names(gate)


@needs_owncast
def test_coming_back_in_the_session_gives_the_same_chat_user_a_new_session_a_new_one(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    first = handoff_token(login(client))
    assert handoff_token(login(client)) == first  # second device in the same session
    bot(client, "POST")  # a new session gets a fresh user, named from Twitch again
    assert handoff_token(login(client)) != first
    assert len(gate.state.chatters) == 1


@needs_owncast
def test_a_user_disabled_in_the_chat_stays_disabled(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    first = handoff_token(login(client))
    oc_id = gate.state.chatters["7"]["oc_id"]
    auth = base64.b64encode(f"admin:{OWNCAST_PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        f"{OWNCAST}/api/admin/chat/users/setenabled",
        method="POST",
        data=json.dumps({"userId": oc_id, "enabled": False}).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req).close()
    bot(client, "POST")  # and in the next session
    again = login(client)
    assert again.status_code == 200 and can_watch(client)  # watching is allowed
    assert handoff_token(again) == first  # but it is the same, disabled user
    assert gate.state.chatters["7"]["oc_id"] == oc_id


@needs_owncast
def test_a_lost_chat_user_is_registered_again(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    gate.state.chatters["7"]["oc_token"] = "gone"
    fresh = handoff_token(login(client))
    assert fresh != "gone" and gate.state.chatters["7"]["oc_token"] == fresh


@needs_owncast
def test_a_display_name_in_another_alphabet_falls_back_to_the_login(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    fake_twitch.user = {"id": "8", "login": "vasya", "display_name": "Вася"}
    login(client)
    assert "vasya" in names(gate)


def test_the_next_page_cannot_leave_the_site(client, fake_twitch):
    bot(client, "POST")
    r = client.get("/_gate/twitch?next=//evil.example")
    cookie = r.cookies.get("kzt") or client.cookies.get("kzt")
    raw = base64.urlsafe_b64decode(cookie.partition(".")[0]).decode()
    assert raw.endswith("|/")


@needs_owncast
def test_twitch_alone_does_not_let_in_the_code_is_still_asked(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.follows = True
    r = twitch_step(client)
    assert r.status_code == 303 and r.headers["location"].startswith("/_gate/enter")
    assert not can_watch(client)
    page_ = client.get("/_gate/enter")
    assert "Twitch: <b>Gopnik</b>" in page_.text and "Теперь код из чата Twitch" in page_.text
    assert "Войти через Twitch" not in page_.text
    wrong = client.post("/_gate/enter", data={"code": "x", "next": "/"})
    assert wrong.status_code == 401 and "Twitch: <b>Gopnik</b>" in wrong.text and not can_watch(client)
    code = bot(client, "GET").json()["code"]
    right = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert right.status_code == 200 and handoff_token(right) and can_watch(client)


@needs_owncast
def test_a_twitch_step_from_an_old_session_does_not_count(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.follows = True
    twitch_step(client)
    code = bot(client, "POST").json()["code"]  # new session
    assert "Twitch:" not in client.get("/_gate/enter").text
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 401 and "Сначала войди через Twitch" in r.text and not can_watch(client)


def test_the_login_asks_for_the_write_right(client, fake_twitch):
    bot(client, "POST")
    q = parse_qs(urlparse(client.get("/_gate/twitch").headers["location"]).query)
    assert "user:write:chat" in q["scope"][0].split()


@needs_owncast
def test_the_page_learns_that_the_viewer_lost_the_voice_and_twitch_brings_it_back(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert client.get("/_gate/live").json()["voice"] is True
    gate.state.voices.clear()  # a gate restart loses the tokens, which live only in memory
    assert client.get("/_gate/live").json()["voice"] is False
    r = twitch_step(client)  # "Reconnect" goes straight to chat, as the code is already entered
    assert r.status_code == 200 and handoff_token(r)
    assert client.get("/_gate/live").json()["voice"] is True


def test_signing_in_as_another_account_issues_the_entry_cookie_again(client, fake_twitch, gate, monkeypatch):
    async def identity(user):  # like the real one minus Owncast, it fills chatters
        gate.state.chatters[user["id"]] = {
            "login": user["login"],
            "name": user["display_name"],
            "oc_id": f"oc{user['id']}",
            "oc_token": f"tok{user['id']}",
            "sid": gate.state.session["sid"],
        }
        return gate.state.chatters[user["id"]]

    monkeypatch.setattr(gate.owncast, "chat_identity", identity)
    bot(client, "POST")
    fake_twitch.follows = True
    assert handoff_token(login(client)) == "tok7"
    assert client.cookies["kz"].split(".")[2] == "7"
    fake_twitch.user = {"id": "8", "login": "tolik", "display_name": "Tolik"}
    r = twitch_step(client)  # the code is already entered, so straight to chat
    assert handoff_token(r) == "tok8"
    assert r.cookies["kz"].split(".")[2] == "8"  # the ban check reads the account that is chatting
    gate.state.bans["id:8"] = {"login": "tolik", "name": "Tolik", "oc_ids": ["oc8"], "at": 0}
    assert client.get("/_gate/auth").status_code == 302


def test_the_return_address_survives_the_twitch_round_trip(client, fake_twitch, fake_chat):
    bot(client, "POST")
    fake_twitch.follows = True
    r = twitch_step(client, "/embed/chat/readwrite/")
    assert r.headers["location"] == "/_gate/enter?next=/embed/chat/readwrite/"


@pytest.mark.parametrize("answer", [(200, {"scope": []}), (200, None), (500, {"access_token": "user-token"})])
def test_a_token_answer_without_a_usable_token_is_refused(client, fake_twitch, gate, monkeypatch, answer):
    bot(client, "POST")
    real = gate.upstream.http_sync

    def tokenless(method, url, **kw):
        if url.endswith("/oauth2/token") and kw["data"]["grant_type"] == "authorization_code":
            return answer
        return real(method, url, **kw)

    monkeypatch.setattr(gate.upstream, "http_sync", tokenless)
    assert login(client).status_code == 400 and not can_watch(client)


USER = {"data": [{"id": "7", "login": "gopnik", "display_name": "Gopnik"}]}


@pytest.mark.parametrize("who", ["viewer", "channel"])
@pytest.mark.parametrize("answer", [(200, {"data": []}), (200, None), (500, USER)])
def test_an_unusable_twitch_user_answer_is_an_outage_and_the_token_is_revoked(
    client, fake_twitch, gate, monkeypatch, who, answer
):
    """A Helix answer with no user is Upstream: the viewer is told Twitch is unavailable, not shown a 500."""
    bot(client, "POST")
    real = gate.upstream.http_sync

    def empty(method, url, **kw):
        u = urlparse(url)
        if u.path == "/helix/users" and ("login=" in u.query) == (who == "channel"):
            return answer
        return real(method, url, **kw)

    monkeypatch.setattr(gate.upstream, "http_sync", empty)
    assert login(client).status_code == 502 and not can_watch(client)
    assert fake_twitch.revoked == ["user-token"]


@pytest.mark.parametrize("streamer", [True, False])
def test_the_broadcaster_badge_goes_to_the_streamer_only(client, fake_twitch, fake_chat, gate, streamer):
    bot(client, "POST")
    fake_twitch.follows = True
    if streamer:
        fake_twitch.user = {"id": CHANNEL_ID, "login": "exitfound", "display_name": "exitfound"}
    login(client)
    badges = gate.state.look["badges"]
    assert badges == ({"exitfound": ["broadcaster/1"]} if streamer else {})


@pytest.mark.parametrize("may_write", [True, False])
def test_the_token_is_kept_as_the_voice_only_with_the_write_right(client, fake_twitch, fake_chat, gate, may_write):
    """With user:write:chat the token stays in memory for hall lines; without it, it is revoked at once."""
    bot(client, "POST")
    fake_twitch.follows = True
    if not may_write:
        fake_twitch.scopes = ["user:read:follows", "user:read:subscriptions"]
    assert login(client).status_code == 200 and can_watch(client)
    assert bool(gate.state.voices) is may_write
    assert fake_twitch.revoked == ([] if may_write else ["user-token"])
