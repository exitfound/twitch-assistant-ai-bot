"""Gate tests: code login, Twitch login, the chat bridge API for the bot.

Twitch is faked (fake_twitch), Owncast is real: OWNCAST_URL points to a local
owncast/owncast:0.3.0 with the admin password OWNCAST_ADMIN_PASSWORD. Without it
the chat tests are skipped.

    docker run -d --name oc-test -p 127.0.0.1:18080:8080 owncast/owncast:0.3.0 -adminpassword testpass
    OWNCAST_URL=http://127.0.0.1:18080 OWNCAST_ADMIN_PASSWORD=testpass python -m pytest test_app.py
"""
import asyncio
import base64
import importlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from websockets.sync.client import connect

HERE = Path(__file__).parent
OWNCAST = os.environ.get("OWNCAST_URL", "")
OWNCAST_PASSWORD = os.environ.get("OWNCAST_ADMIN_PASSWORD", "")
needs_owncast = pytest.mark.skipif(not OWNCAST, reason="no local Owncast (OWNCAST_URL)")


def _owncast_live() -> bool:
    try:
        with urllib.request.urlopen(f"{OWNCAST}/api/status", timeout=3) as r:
            return bool(json.loads(r.read()).get("online"))
    except (OSError, ValueError):
        return False


# Owncast drops viewers' chat lines while offline: tests where a viewer writes need a live stream
needs_stream = pytest.mark.skipif(not (OWNCAST and _owncast_live()),
                                  reason="no stream in the local Owncast: it drops viewers' lines")

TOKEN = "bot-token"
CHANNEL_ID = "100"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_SECRET", "s3cret")
    monkeypatch.setenv("GATE_API_TOKEN", TOKEN)
    monkeypatch.setenv("GATE_STATE", str(tmp_path / "session.json"))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("TWITCH_CHANNEL", "exitfound")
    monkeypatch.setenv("GATE_PUBLIC_URL", "https://kinozal.kube.how")
    monkeypatch.setenv("OWNCAST_URL", OWNCAST or "http://owncast.invalid")
    monkeypatch.setenv("OWNCAST_ADMIN_PASSWORD", OWNCAST_PASSWORD)
    sys.path.insert(0, str(HERE))
    import app
    app = importlib.reload(app)
    yield app
    sys.path.remove(str(HERE))


@pytest.fixture
def client(gate):
    # https: TestClient returns a secure=True cookie only over https
    return TestClient(gate.app, base_url="https://testserver", follow_redirects=False)


def bot(client, method, **kw):
    return client.request(method, "/_gate/api/session", headers={"Authorization": f"Bearer {TOKEN}"}, **kw)


@pytest.fixture
def fake_twitch(gate, monkeypatch):
    """Scripted Twitch: who logged in, follower or not, subscriber or not. Owncast requests go to the real one."""
    real = gate._http
    tw = type("Twitch", (), {})()
    tw.user = {"id": "7", "login": "gopnik", "display_name": "Gopnik"}
    tw.follows, tw.sub, tw.token_status = False, False, 200
    tw.scopes = ["user:read:follows", "user:read:subscriptions", "user:write:chat"]
    tw.revoked, tw.calls, tw.said = [], [], []
    tw.send = []  # Helix send responses in order: (status, is_sent); empty – accepted
    tw.refresh_ok, tw.refreshes = True, 0
    tw.color = "#FF4500"  # viewer nick colour in Twitch chat; "" – not set
    tw.emotes_status = 200

    def fake(method, url, *, data=None, json_body=None, headers=None):
        host = urlparse(url).netloc
        if host not in ("id.twitch.tv", "api.twitch.tv"):
            return real(method, url, data=data, json_body=json_body, headers=headers)
        path, q = urlparse(url).path, parse_qs(urlparse(url).query)
        tw.calls.append(path)
        if path == "/oauth2/token" and data["grant_type"] == "client_credentials":
            return 200, {"access_token": "app-token", "expires_in": 5000000}
        if headers and headers.get("Authorization") == "Bearer app-token":
            if path == "/helix/users" and q.get("login") == ["exitfound"]:
                return 200, {"data": [{"id": CHANNEL_ID, "login": "exitfound"}]}
            if path == "/helix/chat/emotes/global":
                return tw.emotes_status, {"data": [{"id": "25", "name": "Kappa"}, {"id": "bad id", "name": "Broken"}]}
            if path == "/helix/chat/emotes":
                assert q["broadcaster_id"] == [CHANNEL_ID]
                return 200, {"data": [{"id": "emotesv2_abc123", "name": "exitfoLove"}]}
            if path == "/helix/chat/badges/global":
                return 200, {"data": [
                    {"set_id": "moderator", "versions": [{"id": "1", "title": "Moderator",
                                                          "image_url_1x": "https://b/mod1", "image_url_2x": "https://b/mod2"}]},
                    {"set_id": "subscriber", "versions": [{"id": "0", "title": "Subscriber",
                                                           "image_url_1x": "https://b/gsub", "image_url_2x": "https://b/gsub2"}]},
                    {"set_id": "vip", "versions": [{"id": "1", "title": "VIP", "image_url_1x": "https://b/vip"}]}]}
            if path == "/helix/chat/badges":
                assert q["broadcaster_id"] == [CHANNEL_ID]
                return 200, {"data": [{"set_id": "subscriber", "versions": [
                    {"id": "0", "title": "Subscriber", "image_url_1x": "https://b/csub", "image_url_2x": "https://b/csub2"}]}]}
            raise AssertionError(url)
        if path == "/oauth2/token" and data["grant_type"] == "refresh_token":
            tw.refreshes += 1
            if not tw.refresh_ok:
                return 400, {"message": "Invalid refresh token"}
            return 200, {"access_token": f"user-token-{tw.refreshes + 1}", "refresh_token": "user-refresh",
                         "expires_in": 14000}
        if path == "/oauth2/token":
            assert data["client_secret"] == "csecret" and data["code"] == "good-code"
            if tw.token_status != 200:
                return tw.token_status, None
            return 200, {"access_token": "user-token", "refresh_token": "user-refresh", "expires_in": 14000,
                         "scope": tw.scopes}
        if path == "/oauth2/revoke":
            tw.revoked.append(data["token"])
            return 200, None
        if path == "/helix/chat/messages":
            status, is_sent = tw.send.pop(0) if tw.send else (200, True)
            tw.said.append((headers["Authorization"], json_body))
            reason = None if is_sent else {"code": "msg_rejected", "message": "AutoMod"}
            return status, {"data": [{"message_id": "m", "is_sent": is_sent, "drop_reason": reason}]}
        assert headers["Authorization"] == "Bearer user-token" and headers["Client-Id"] == "cid"
        if path == "/helix/users" and "login" in q:
            return 200, {"data": [{"id": CHANNEL_ID, "login": "exitfound"}]}
        if path == "/helix/users":
            return 200, {"data": [tw.user]}
        if path == "/helix/chat/color":
            assert q["user_id"] == [tw.user["id"]]
            return 200, {"data": [{"user_id": tw.user["id"], "color": tw.color}]}
        if path == "/helix/channels/followed":
            assert q["broadcaster_id"] == [CHANNEL_ID] and q["user_id"] == [tw.user["id"]]
            return 200, {"data": [{"broadcaster_id": CHANNEL_ID}] if tw.follows else []}
        if path == "/helix/subscriptions/user":
            return (200, {"data": [{}]}) if tw.sub else (404, None)
        raise AssertionError(url)

    monkeypatch.setattr(gate, "_http", fake)
    monkeypatch.setattr(gate, "_revoke_later", lambda tokens: tw.revoked.extend(tokens))
    return tw


@pytest.fixture
def fake_chat(gate, monkeypatch):
    """Owncast chat registration without Owncast: each Twitch viewer gets a chat user of their own."""
    async def identity(user):
        record = {"login": user["login"], "name": user["display_name"], "oc_id": f"oc-{user['id']}",
                  "oc_token": f"oc-token-{user['id']}", "sid": gate.session["sid"] if gate.session else None}
        gate.chatters[user["id"]] = record
        return record
    monkeypatch.setattr(gate, "chat_identity", identity)


def login(client, nxt="/"):
    """Button → Twitch → back → session code. Returns the code response or the callback refusal."""
    r = twitch_step(client, nxt)
    if r.status_code != 303 or not r.headers["location"].startswith("/_gate/enter"):
        return r
    code = bot(client, "GET").json()["code"]
    return client.post("/_gate/enter", data={"code": code, "next": nxt})


def twitch_step(client, nxt="/"):
    """Button → Twitch → back with an authorization code. Returns the callback response."""
    r = client.get(f"/_gate/twitch?next={nxt}")
    assert r.status_code == 303, r.text
    to = urlparse(r.headers["location"])
    assert to.netloc == "id.twitch.tv"
    q = parse_qs(to.query)
    assert q["redirect_uri"] == ["https://kinozal.kube.how/_gate/twitch/callback"]
    assert q["scope"] == ["user:read:follows user:read:subscriptions user:write:chat"]
    return client.get(f"/_gate/twitch/callback?code=good-code&state={q['state'][0]}")


def handoff_token(r) -> str:
    start = r.text.index('setItem("accessToken",') + len('setItem("accessToken",')
    return json.loads(r.text[start:r.text.index(")", start)].replace("\\u003c", "<"))


def can_watch(client) -> bool:
    return client.get("/_gate/auth").status_code == 200


# --- code and session ---

def test_code_entry_and_a_new_session_invalidating_it(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)  # no Twitch app: code-only login
    code = bot(client, "POST", json={"title": "Чужие"}).json()["code"]
    assert client.get("/_gate/auth").status_code == 302
    assert client.post("/_gate/enter", data={"code": "x", "next": "/"}).status_code == 401
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 303
    assert can_watch(client)
    bot(client, "POST")
    assert not can_watch(client)


def test_wrong_codes_lock_the_address_out_for_the_window(client, gate, monkeypatch):
    """A 4-digit code has 10 000 values: past FAIL_LIMIT wrong tries an address waits FAIL_WINDOW,
    even with the right code, and another address is not affected."""
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    code = bot(client, "POST").json()["code"]
    wrong = "0000" if code != "0000" else "1111"
    for _ in range(gate.FAIL_LIMIT):
        assert client.post("/_gate/enter", data={"code": wrong, "next": "/"}).status_code == 401
    locked = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert locked.status_code == 429 and not can_watch(client)
    other = {"x-forwarded-for": "5.6.7.8"}
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}, headers=other).status_code == 303
    monkeypatch.setattr(gate, "LEASE", 0)  # the clock jump below must not close the session
    now = time.time()
    monkeypatch.setattr(gate.time, "time", lambda: now + gate.FAIL_WINDOW + 1)
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 303


def test_wrong_host_keys_lock_the_address_out_too(client, gate):
    for i in range(gate.FAIL_LIMIT):
        assert client.get(f"/_gate/host?key=wrong{i}").status_code == 404
    assert client.get(f"/_gate/host?key={gate.HOST_KEY}").status_code == 429


def test_wrong_codes_count_per_twitch_account_behind_a_shared_address(client, gate, fake_twitch, fake_chat):
    """Viewers behind one carrier NAT share an address: one viewer's typos lock out only that viewer,
    and the address is closed only past FAIL_IP_LIMIT."""
    code = bot(client, "POST").json()["code"]
    wrong = "0000" if code != "0000" else "1111"
    fake_twitch.follows = True
    twitch_step(client)
    for _ in range(gate.FAIL_LIMIT):
        assert client.post("/_gate/enter", data={"code": wrong, "next": "/"}).status_code == 401
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 429
    other = TestClient(gate.app, base_url="https://testserver", follow_redirects=False)
    fake_twitch.user = {"id": "8", "login": "vasya", "display_name": "Vasya"}
    twitch_step(other)
    assert other.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 200
    gate.failures[client_address(gate)] = [time.time()] * gate.FAIL_IP_LIMIT
    third = TestClient(gate.app, base_url="https://testserver", follow_redirects=False)
    fake_twitch.user = {"id": "9", "login": "petya", "display_name": "Petya"}
    twitch_step(third)
    assert third.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 429


def client_address(gate) -> str:
    return gate.client_ip(type("Req", (), {"headers": {}, "client": type("C", (), {"host": "testclient"})()})())


def test_non_ascii_secrets_are_refused_not_crashed_on(client, gate, monkeypatch):
    """compare_digest raises on non-ASCII str: a forged cookie or code must be a refusal, not a 500."""
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    assert not gate.same("é", "e") and gate.same("abc", "abc")
    assert client.post("/_gate/enter", data={"code": "١٢٣٤", "next": "/"}).status_code == 401
    client.cookies.set("kz", "é.é", domain="testserver")
    assert client.get("/_gate/auth").status_code == 302


def test_the_last_forwarded_address_is_the_client(gate):
    """Caddy, with no trusted proxies, writes the real address last: what a client sent before it is ignored."""
    req = type("Req", (), {"headers": {"x-forwarded-for": "1.2.3.4, 9.9.9.9"}, "client": None})()
    assert gate.client_ip(req) == "9.9.9.9"


def test_a_state_file_is_replaced_whole_and_a_broken_one_reads_as_default(gate, tmp_path):
    path = tmp_path / "state.json"
    assert gate.read_json(path, {"empty": True}) == {"empty": True}
    gate.write_json(path, {"ник": "Гопник"})
    assert gate.read_json(path, None) == {"ник": "Гопник"} and not path.with_suffix(".tmp").exists()
    path.write_text("{not json")
    assert gate.read_json(path, []) == []


def test_delete_says_whether_there_was_a_session(client):
    assert bot(client, "DELETE").json()["closed"] is False
    bot(client, "POST")
    assert bot(client, "DELETE").json()["closed"] is True


def test_the_login_page_offers_twitch_only_when_configured(client, gate, monkeypatch):
    bot(client, "POST")
    assert "Войти через Twitch" in client.get("/_gate/enter").text
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    assert "Войти через Twitch" not in client.get("/_gate/enter").text
    assert client.get("/_gate/twitch").headers["location"].startswith("/_gate/enter")


# --- language: Russian by default, English by the kz_lang cookie ---

def test_the_entry_page_is_russian_by_default_and_english_by_the_cookie(client, gate):
    bot(client, "POST")
    ru = client.get("/_gate/enter").text
    assert '<html lang="ru">' in ru and "Войти через Twitch" in ru
    assert 'aria-current="true" title="Русский">RU</span>' in ru and 'hreflang="en"' in ru and ">EN</a>" in ru
    client.cookies.set("kz_lang", "en")
    en = client.get("/_gate/enter").text
    assert '<html lang="en">' in en and "Sign in with Twitch" in en
    assert 'aria-current="true" title="English">EN</span>' in en and ">RU</a>" in en
    # only the switch and the link preview carry Russian on the English page
    rest = en.replace('"Русский"', "").replace('"Переключить на русский"', "").replace('"Язык / Language"', "")
    rest = rest.replace(gate.PREVIEW_TITLE, "").replace(gate.PREVIEW_SUMMARY, "")
    assert "<title>Cinema</title>" in en and not any("\u0400" <= c <= "\u04ff" for c in rest)
    client.cookies.set("kz_lang", "xx")  # unknown: Russian
    assert '<html lang="ru">' in client.get("/_gate/enter").text


def test_without_a_choice_the_browser_language_decides(client, gate):
    bot(client, "POST")
    lang = lambda header: client.get("/_gate/enter", headers={"Accept-Language": header}).text.split('"')[1]  # noqa: E731
    assert lang("en-US,en;q=0.9") == "en"
    assert lang("uk-UA,uk;q=0.9,en;q=0.8") == "ru"  # a CIS language reads Russian
    assert lang("de-DE,ru;q=0.5") == "en"  # the preferred one decides, not any listed
    assert lang("en;q=0.3,ru;q=0.8") == "ru"  # by q, not by order
    assert lang("") == "ru" and lang("*") == "ru"
    client.cookies.set("kz_lang", "ru")  # an explicit choice beats the browser
    assert lang("en-US") == "ru"


def test_the_language_switch_remembers_the_choice_and_returns_to_the_entry_page(client, gate):
    r = client.get("/_gate/lang?to=en&next=/embed/chat/readwrite/")
    assert r.status_code == 303 and r.headers["location"] == "/_gate/enter?next=/embed/chat/readwrite/"
    assert r.cookies["kz_lang"] == "en"
    assert "httponly" not in r.headers["set-cookie"].lower()  # custom.js reads and sets it too
    assert client.get("/_gate/lang?to=xx&next=//evil.example").headers["location"] == "/_gate/enter?next=/"


def test_errors_follow_the_language(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.cookies.set("kz_lang", "en")
    r = client.post("/_gate/enter", data={"code": "nope", "next": "/"})
    assert r.status_code == 401 and "Wrong code." in r.text


def test_the_site_opens_in_the_viewer_s_language(client, gate, monkeypatch):
    # Owncast takes its own language only from ?lang=, so the gate adds it for English and drops it for Russian
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    code = bot(client, "GET").json()["code"]
    client.cookies.set("kz_lang", "en")
    r = client.post("/_gate/enter", data={"code": code, "next": "/?x=1"})
    assert r.headers["location"] == "/?x=1&lang=en"
    client.cookies.set("kz_lang", "ru")
    assert client.get("/_gate/enter?next=/%3Flang%3Den").headers["location"] == "/"
    assert gate.with_lang("/embed/chat/readwrite/", "en") == "/embed/chat/readwrite/?lang=en"


# --- Twitch login: who gets in ---

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
    real = gate._http

    def down(method, url, **kw):
        if "channels/followed" in url:
            raise gate.Upstream("timeout")
        return real(method, url, **kw)
    monkeypatch.setattr(gate, "_http", down)
    r = login(client)
    assert r.status_code == 502 and not can_watch(client)
    assert fake_twitch.revoked == ["user-token"]


def test_a_bad_code_exchange_is_refused(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.token_status = 400
    assert login(client).status_code == 400 and not can_watch(client)


@needs_owncast
@pytest.mark.parametrize("who", ["follower", "sub", "broadcaster"])
def test_followers_subs_and_the_streamer_get_in_with_their_chat_name(client, fake_twitch, who):
    bot(client, "POST")
    if who == "follower":
        fake_twitch.follows = True
    elif who == "sub":
        fake_twitch.sub = True
    else:
        fake_twitch.user = {"id": CHANNEL_ID, "login": "exitfound", "display_name": "ExitFound"}
    r = login(client, nxt="/embed/chat/readwrite/")
    assert r.status_code == 200, r.text
    assert 'location.replace("/embed/chat/readwrite/")' in r.text
    assert can_watch(client)
    assert fake_twitch.revoked == []  # token kept: hall lines go to Twitch with it
    # the token from the page is a real Owncast chat user with the Twitch nick
    token = handoff_token(r)
    with urllib.request.urlopen(f"{OWNCAST}/api/chat?accessToken={token}") as resp:
        assert resp.status == 200
    assert fake_twitch.user["display_name"] in names(gate_of(client))


def gate_of(client):
    return sys.modules["app"]


def names(gate) -> set:
    return {c["name"] for c in gate.chatters.values()}


@needs_owncast
def test_coming_back_in_the_session_gives_the_same_chat_user_a_new_session_a_new_one(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.follows = True
    first = handoff_token(login(client))
    assert handoff_token(login(client)) == first  # second device in the same session
    bot(client, "POST")  # next session: a fresh user, the nick again from Twitch
    assert handoff_token(login(client)) != first
    assert len(gate_of(client).chatters) == 1


@needs_owncast
def test_a_user_disabled_in_the_chat_stays_disabled(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    first = handoff_token(login(client))
    oc_id = gate.chatters["7"]["oc_id"]
    auth = base64.b64encode(f"admin:{OWNCAST_PASSWORD}".encode()).decode()
    req = urllib.request.Request(f"{OWNCAST}/api/admin/chat/users/setenabled", method="POST",
                                 data=json.dumps({"userId": oc_id, "enabled": False}).encode(),
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
    urllib.request.urlopen(req).close()
    bot(client, "POST")  # and in the next session
    again = login(client)
    assert again.status_code == 200 and can_watch(client)  # watching is allowed
    assert handoff_token(again) == first  # but it is the same, disabled user
    assert gate.chatters["7"]["oc_id"] == oc_id


@needs_owncast
def test_a_lost_chat_user_is_registered_again(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    gate.chatters["7"]["oc_token"] = "gone"
    fresh = handoff_token(login(client))
    assert fresh != "gone" and gate.chatters["7"]["oc_token"] == fresh


@needs_owncast
def test_a_display_name_in_another_alphabet_falls_back_to_the_login(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.follows = True
    fake_twitch.user = {"id": "8", "login": "vasya", "display_name": "Вася"}
    login(client)
    assert "vasya" in names(gate_of(client))


def test_the_next_page_cannot_leave_the_site(client, fake_twitch):
    bot(client, "POST")
    r = client.get("/_gate/twitch?next=//evil.example")
    cookie = r.cookies.get("kzt") or client.cookies.get("kzt")
    raw = base64.urlsafe_b64decode(cookie.partition(".")[0]).decode()
    assert raw.endswith("|/")


@pytest.mark.parametrize("nxt", ["/\t/evil.example", "/\n/evil.example", "/ /evil.example", "/x\x7f"])
def test_a_control_character_cannot_turn_the_next_page_into_another_site(client, gate, nxt):
    """urlparse and browsers drop a tab or a newline: «/<tab>/evil» would become «//evil»."""
    assert gate.safe_next(nxt) == "/"
    assert gate.with_lang(gate.safe_next(nxt), "en") == "/?lang=en"
    r = client.get("/_gate/lang", params={"to": "en", "next": nxt})
    assert r.headers["location"] == "/_gate/enter?next=/"


def test_the_entry_page_carries_the_link_preview(client, gate):
    """A link-preview crawler has no cookie: Caddy sends it to the entry page, so the card lives there."""
    page_ = client.get("/_gate/enter").text
    assert '<meta property="og:title" content="Кинозал · Cinema">' in page_
    assert ('<meta property="og:description" content="Кино со стрима exitfound · Films from the exitfound stream">'
            in page_)


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
    assert right.status_code == 200 and 'setItem("accessToken"' in right.text and can_watch(client)


@needs_owncast
def test_a_twitch_step_from_an_old_session_does_not_count(client, fake_twitch):
    bot(client, "POST")
    fake_twitch.follows = True
    twitch_step(client)
    code = bot(client, "POST").json()["code"]  # new session
    assert "Twitch:" not in client.get("/_gate/enter").text
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 401 and "Сначала войди через Twitch" in r.text and not can_watch(client)


def test_a_code_without_twitch_is_refused(client):
    code = bot(client, "POST").json()["code"]
    page_ = client.get("/_gate/enter")
    assert "Войти через Twitch" in page_.text and 'name="code"' not in page_.text  # no code field yet
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 401 and "Сначала войди через Twitch" in r.text and not can_watch(client)


def test_without_a_twitch_app_the_code_alone_lets_in(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    code = bot(client, "POST").json()["code"]
    assert 'name="code"' in client.get("/_gate/enter").text
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 303 and can_watch(client)


def test_a_mention_from_the_owncast_picker_stays_a_mention(gate):
    assert gate.plain("<p>@<br>\ndv4mp1r3 ты где</p>") == "@dv4mp1r3 ты где"
    assert gate.plain("<p>почта a @ b</p>") == "почта a @b"


def test_the_handoff_cannot_break_out_of_the_script(gate):
    r = gate.chat_handoff('x"</script><script>alert(1)//', "/")
    assert "</script><script>alert" not in r.body.decode()


def test_the_chat_bridge_needs_the_bot_token(client):
    assert client.post("/_gate/api/chat").status_code == 401
    assert client.post("/_gate/api/chat", headers={"Authorization": "Bearer nope"}).status_code == 401


# --- chat bridge ---

def chat(client, method="POST", **kw):
    return client.request(method, "/_gate/api/chat", headers={"Authorization": f"Bearer {TOKEN}"}, **kw)


def say(token: str, *texts: str, rename: str | None = None) -> None:
    """A viewer writes to the Owncast chat over the websocket, like a browser."""
    import time
    with connect(f"{OWNCAST.replace('http', 'ws')}/ws?accessToken={token}") as ws:
        time.sleep(0.3)
        if rename:
            ws.send(json.dumps({"type": "NAME_CHANGE", "newName": rename}))
            time.sleep(1.1)
        for text in texts:
            ws.send(json.dumps({"type": "CHAT", "body": text}))
            time.sleep(1.1)  # Owncast flood protection


def test_markdown_from_twitch_is_shown_as_text(gate):
    assert gate.escape_markdown("a_b *c* [d](e) <b> `f` ~g~ \\ 1.5") == \
        "a\\_b \\*c\\* \\[d\\](e) \\<b\\> \\`f\\` \\~g\\~ \\\\ 1.5"


def test_the_reply_mark_does_not_go_to_twitch(gate):
    body = "<p>@anna\u2063\u200b\u200c\u200d\u2060\u2063 согласен</p>"
    assert gate.plain(body) == "@anna согласен"
    # a partial mark (end or start erased with Backspace) is stripped too
    assert gate.plain("<p>@anna\u2063\u200b\u200c согласен</p>") == "@anna согласен"
    assert gate.plain("<p>@anna\u200b\u200c\u2063 согласен</p>") == "@anna согласен"
    # a compound emoji joiner without an adjacent U+2063 stays
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    assert gate.plain(f"<p>{family} семья</p>") == f"{family} семья"


def test_owncast_html_becomes_plain_text(gate):
    body = '<p>привет <strong>всем</strong> &amp; <a href="https://t.co">https://t.co</a> <img alt=":kek:" src="x"></p>'
    assert gate.plain(body) == "привет всем & https://t.co :kek:"


def anonymous_token() -> str:
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{OWNCAST}/api/chat/register", method="POST", data=b'{"displayName":"anon"}',
        headers={"Content-Type": "application/json"})).read())["accessToken"]


def hall_history(gate) -> list:
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{OWNCAST}/api/integrations/chat", headers={"Authorization": f"Bearer {gate.chat_token}"})).read())


def by(gate, name) -> list:
    asyncio.run(gate.owncast_chat_token())
    return [m for m in hall_history(gate) if (m.get("user") or {}).get("displayName") == name]


@needs_owncast
def test_twitch_lines_reach_the_hall_from_the_author_s_twin(client, gate):
    bot(client, "POST")
    assert chat(client, "POST", json={"body": "Gopnik: **привет** из твича @dv4mp1r3"}).json() == {"sent": True}
    assert chat(client, "POST", json={"user": "Gopnik", "body": "второе"}).json() == {"sent": True}
    lines = by(gate, "Gopnik [TW]")[-2:]  # Owncast is shared by all tests
    assert lines[0]["user"]["isBot"]
    assert "**привет**" in lines[0]["body"] and "<strong>@dv4mp1r3</strong>" in lines[0]["body"]
    assert "Gopnik:" not in lines[0]["body"] and "второе" in lines[1]["body"]
    assert list(gate.twins) == ["gopnik"]  # one twin per viewer


@needs_owncast
def test_a_twitch_viewer_s_twin_does_not_take_the_hall_name(client, gate):
    bot(client, "POST")
    chat(client, "POST", json={"body": "Tolik: привет"})
    reg = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{OWNCAST}/api/chat/register", method="POST", data=b'{"displayName":"Tolik"}',
        headers={"Content-Type": "application/json"})).read())
    assert reg["displayName"] == "Tolik"


@needs_owncast
def test_without_a_twin_the_line_goes_from_twitch(client, gate, monkeypatch):
    bot(client, "POST")

    async def broken(name, fresh=False):
        raise gate.Upstream("nope")
    monkeypatch.setattr(gate, "twin_token", broken)
    assert chat(client, "POST", json={"body": "gopnik_1: **привет**"}).json() == {"sent": True}
    assert "gopnik_1: **привет**" in by(gate, "Twitch")[-1]["body"]


def test_the_hall_loop_survives_failures_and_logs_an_outage_once(client, gate, monkeypatch, capsys):
    """Owncast down for several rounds is one log line; a malformed line does not stop the loop;
    bans are synced on the first round of a session, then once per BAN_SYNC_SECONDS."""
    bot(client, "POST")
    monkeypatch.setattr(gate, "HALL_POLL_SECONDS", 0)
    monkeypatch.setattr(gate, "BAN_SYNC_SECONDS", 3600)
    rounds = [gate.Upstream("owncast down"), gate.Upstream("owncast down"), ValueError("bad line"), None,
              gate.Upstream("down again"), asyncio.CancelledError()]
    synced = []

    async def relay():
        result = rounds.pop(0)
        if result is not None:
            raise result

    async def sync():
        synced.append(True)
    monkeypatch.setattr(gate, "relay_once", relay)
    monkeypatch.setattr(gate, "sync_bans", sync)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gate.hall_loop())
    out = capsys.readouterr().out
    assert out.count("owncast down") == 1 and "bad line" in out and "down again" in out
    assert synced == [True]


def test_the_hall_loop_survives_any_failure_of_the_ban_sync_and_the_lease(client, gate, monkeypatch, capsys):
    """Only the loop relays the hall: a ban sync or a lease check that fails in any way is logged, not fatal."""
    bot(client, "POST")
    monkeypatch.setattr(gate, "HALL_POLL_SECONDS", 0)
    monkeypatch.setattr(gate, "BAN_SYNC_SECONDS", 0)
    syncs = [KeyError("oc_ids"), gate.Upstream("owncast down")]
    relayed = []

    async def sync():
        if syncs:
            raise syncs.pop(0)

    async def relay():
        relayed.append(True)
        if len(relayed) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(gate, "sync_bans", sync)
    monkeypatch.setattr(gate, "relay_once", relay)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gate.hall_loop())
    out = capsys.readouterr().out
    assert "KeyError" in out and "owncast down" in out and len(relayed) == 3


def test_a_peer_dropping_the_answer_is_an_upstream_failure(gate, monkeypatch):
    """Owncast restarting mid-answer raises http.client errors, which must not escape as anything else."""
    from http.client import IncompleteRead

    def cut(*args, **kwargs):
        raise IncompleteRead(b"[")
    monkeypatch.setattr(gate.urllib.request, "urlopen", cut)
    with pytest.raises(gate.Upstream):
        gate._http("GET", "http://owncast.invalid/api/admin/chat/users/disabled")


def test_an_unexplained_401_does_not_fall_back_to_the_shared_sender(client, gate, monkeypatch):
    """Owncast refused the twin and the ban check failed: the author may be banned, so the line is dropped."""
    bot(client, "POST")
    sent = []

    async def token(name, fresh=False):
        return "twin-token"

    async def owncast(method, url, **kw):
        if url.endswith("/api/integrations/chat/send"):
            sent.append(kw["headers"]["Authorization"])
            return 401, None
        raise gate.Upstream("admin chat history timed out")

    async def shared(*args, **kwargs):
        raise AssertionError("the line went out through the shared «Twitch» user")
    monkeypatch.setattr(gate, "twin_token", token)
    monkeypatch.setattr(gate, "http", owncast)
    monkeypatch.setattr(gate, "integration", shared)
    r = chat(client, json={"body": "troll: привет"})
    assert r.status_code == 502 and sent == ["Bearer twin-token"]


def test_mentions_are_bold_and_markdown_is_text(gate):
    assert gate.hall_markdown("@m1nd_sh *x* a@b.c @ ") == "**@m1nd\\_sh** \\*x\\* a@b.c @ "


@needs_owncast
@needs_stream
def test_a_renamed_viewer_gets_the_twitch_name_back(client, fake_twitch, gate, capsys):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.relay_once())
    oc_id = gate.chatters["7"]["oc_id"]

    def name_now():  # history returns the author's current nick
        return [m["user"]["displayName"] for m in hall_history(gate) if m["user"]["id"] == oc_id][-1]
    say(token, "я теперь root", rename="root")
    assert name_now() == "root"
    asyncio.run(gate.relay_once())
    assert "ник «root» в кинозале – обратно Gopnik" in capsys.readouterr().out
    assert name_now() == "Gopnik"
    asyncio.run(gate.relay_once())  # already Gopnik: left alone
    assert "обратно" not in capsys.readouterr().out


@needs_owncast
@needs_stream
def test_hall_lines_go_to_twitch_as_the_viewer(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    say(token, "сказано до шлюза")
    asyncio.run(gate.relay_once())  # first pass in a session: older lines are not replayed
    assert fake_twitch.said == []
    say(token, "привет из кинозала", rename="exitfound")  # renamed, still the same viewer
    say(anonymous_token(), "я по коду")  # no Twitch login: not sent to Twitch
    asyncio.run(gate.relay_once())
    asyncio.run(gate.relay_once())  # repeated pass: no duplicates
    assert fake_twitch.said == [("Bearer user-token", {
        "broadcaster_id": CHANNEL_ID, "sender_id": "7", "message": "🎬 [KZ] привет из кинозала"})]


@needs_owncast
@needs_stream
def test_a_dead_access_token_is_refreshed_once(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.relay_once())
    fake_twitch.send = [(401, False)]
    say(token, "после истечения")
    asyncio.run(gate.relay_once())
    assert [auth for auth, _ in fake_twitch.said] == ["Bearer user-token", "Bearer user-token-2"]
    assert fake_twitch.refreshes == 1


@needs_owncast
@needs_stream
def test_a_token_twitch_no_longer_takes_drops_the_voice(client, fake_twitch, gate, capsys):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.relay_once())
    fake_twitch.send, fake_twitch.refresh_ok = [(401, False)], False
    say(token, "раз")
    asyncio.run(gate.relay_once())
    assert gate.voices == {}
    assert "голос забыт" in capsys.readouterr().out


@needs_owncast
@needs_stream
def test_a_line_twitch_refuses_is_only_logged(client, fake_twitch, gate, capsys):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.relay_once())
    fake_twitch.send = [(200, False)]
    say(token, "плохое")
    asyncio.run(gate.relay_once())
    assert "AutoMod" in capsys.readouterr().out
    assert gate.voices  # the voice stays


@needs_owncast
@pytest.mark.parametrize("end", ["stop", "new session", "lease"])
def test_the_session_end_forgets_and_revokes_the_voices(client, fake_twitch, gate, monkeypatch, end):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert gate.voices and fake_twitch.revoked == []
    if end == "stop":
        bot(client, "DELETE")
    elif end == "new session":
        bot(client, "POST")
    else:
        gate.session["seen"] -= gate.LEASE + 1
        gate.expire()
    assert gate.voices == {}
    assert fake_twitch.revoked == ["user-token"]


@pytest.mark.parametrize("end", ["stop", "new session"])
def test_a_session_ending_during_the_sign_in_keeps_no_voice(client, fake_twitch, fake_chat, gate, monkeypatch, end):
    """The sign-in waits on Twitch and Owncast: a session closed meanwhile shows the closed page, and the
    viewer's token is revoked rather than kept past forget_voices()."""
    bot(client, "POST")
    fake_twitch.follows = True
    real = gate.may_watch

    async def watch(*args):
        # what DELETE and POST /api/session do; the client itself cannot be called from inside the app
        gate.session = None if end == "stop" else {**gate.session, "sid": "next-session"}
        gate.forget_voices()
        return await real(*args)
    monkeypatch.setattr(gate, "may_watch", watch)
    r = twitch_step(client)
    assert r.status_code == 200 and gate.TEXT["ru"]["closed_lead"] in r.text
    assert not gate.voices and "user-token" in fake_twitch.revoked


@needs_owncast
def test_without_the_write_right_the_token_is_revoked_at_once(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    fake_twitch.scopes = ["user:read:follows", "user:read:subscriptions"]
    assert login(client).status_code == 200 and can_watch(client)
    assert gate.voices == {}
    assert fake_twitch.revoked == ["user-token"]


@needs_owncast
def test_a_second_login_replaces_the_voice(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    login(client)
    assert len(gate.voices) == 1
    assert fake_twitch.revoked == ["user-token"]  # the same viewer's previous token


def test_the_login_asks_for_the_write_right(client, fake_twitch):
    bot(client, "POST")
    q = parse_qs(urlparse(client.get("/_gate/twitch").headers["location"]).query)
    assert "user:write:chat" in q["scope"][0].split()


@needs_owncast
def test_a_revoked_chat_token_is_taken_again(client, gate):
    gate.chat_token = "revoked"
    asyncio.run(gate.integration("POST", "/api/integrations/chat/send", {"body": "проверка"}))
    assert gate.chat_token != "revoked"


@needs_owncast
def test_a_revoked_twin_token_is_taken_again(client, gate):
    gate.twins["gopnik"] = "revoked"
    assert chat(client, json={"body": "Gopnik: проверка"}).json() == {"sent": True}
    assert gate.twins["gopnik"] != "revoked"


def test_an_empty_line_is_refused(client):
    assert chat(client, "POST", json={"body": "  "}).status_code == 400
    assert chat(client, "POST", content=b"not json").status_code == 400


def test_the_live_counter_knows_the_session_start_only_for_viewers(client, gate, monkeypatch):
    assert client.get("/_gate/live").status_code == 401
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    started = bot(client, "POST", json={"title": "Сталкер"}).json()["started"]
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    r = client.get("/_gate/live").json()
    assert r["started"] == started and abs(r["now"] - started) < 5
    assert r["title"] == "Сталкер"
    bot(client, "PATCH", json={"title": "Солярис"})
    assert client.get("/_gate/live").json()["title"] == "Солярис"  # !film title: shown below the player
    bot(client, "DELETE")
    assert client.get("/_gate/live").status_code == 401  # session ended: the cookie no longer lets in


def test_the_obs_placeholder_sees_the_film_without_signing_in(client):
    r = client.get("/_gate/now")
    assert r.json()["open"] is False and r.headers["access-control-allow-origin"] == "*"
    started = bot(client, "POST", json={"title": "Сталкер"}).json()["started"]
    r = client.get("/_gate/now").json()
    assert r == {"open": True, "title": "Сталкер", "started": started, "now": r["now"]}
    assert "code" not in r
    bot(client, "DELETE")
    r = client.get("/_gate/now").json()
    assert (r["open"], r["title"], r["started"]) == (False, None, None)


@needs_owncast
def test_the_page_learns_that_the_viewer_lost_the_voice_and_twitch_brings_it_back(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert client.get("/_gate/live").json()["voice"] is True
    gate.voices.clear()  # gate restart: viewer tokens lived only in memory
    assert client.get("/_gate/live").json()["voice"] is False
    r = twitch_step(client)  # "Reconnect": the code is already entered, straight to chat
    assert r.status_code == 200 and "accessToken" in r.text
    assert client.get("/_gate/live").json()["voice"] is True


def test_a_viewer_without_twitch_has_no_voice_to_lose(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    assert client.get("/_gate/live").json()["voice"] is None


@needs_owncast
def test_logout_drops_the_cookie_and_revokes_the_viewer_s_twitch_token(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    assert can_watch(client) and gate.voices
    page_ = client.get("/_gate/logout")
    assert 'removeItem("accessToken")' in page_.text and 'location.replace("/_gate/enter")' in page_.text
    assert not can_watch(client)  # cookie cleared
    assert client.post("/_gate/logout", json={"token": "чужой"}).status_code == 204
    assert gate.voices and fake_twitch.revoked == []
    assert client.post("/_gate/logout", json={"token": token}).status_code == 204
    assert gate.voices == {} and fake_twitch.revoked == ["user-token"]



# --- chat look: nick colours and Twitch emotes ---

def test_the_look_is_only_for_those_inside(client):
    assert client.get("/_gate/look").status_code == 401


def test_a_twitch_line_brings_its_author_s_color_and_emotes(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate, "say_as_twin", lambda name, text: asyncio.sleep(0))
    bot(client, "POST")
    assert chat(client, json={"body": "Gopnik: привет Kappa sub7Hype", "color": "#ff0000",
                              "emotes": {"Kappa": "25", "sub7Hype": "emotesv2_ff00"}}).json() == {"sent": True}
    # malformed input is skipped silently: colour not #RRGGBB, id with a space, name with a space
    chat(client, json={"body": "Tolik: привет", "color": "red", "emotes": {"Bad": "1 2", "a b": "3", "Ok": 5}})
    assert gate.look["colors"] == {"gopnik": "#FF0000"}
    assert gate.look["emotes"] == {"Kappa": "25", "sub7Hype": "emotesv2_ff00"}
    assert json.loads(gate.LOOK_FILE.read_text())["colors"] == {"gopnik": "#FF0000"}  # survives a restart


def test_inside_the_look_has_colors_and_twitch_and_channel_emotes(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate, "TWITCH_ENABLED", True)
    gate.remember_look("Gopnik", "#00ff7f", {"sub7Hype": "emotesv2_ff00"})
    look = client.get("/_gate/look").json()
    assert look["colors"] == {"gopnik": "#00FF7F"}
    assert look["emotes"] == {"Kappa": "25", "exitfoLove": "emotesv2_abc123", "sub7Hype": "emotesv2_ff00"}
    assert "{id}" in look["url"]
    calls = len(fake_twitch.calls)
    client.get("/_gate/look")
    assert len(fake_twitch.calls) == calls  # Twitch emotes come from the cache, not fetched per request


def test_without_twitch_emotes_the_look_still_answers(client, gate, fake_twitch, monkeypatch):
    fake_twitch.emotes_status = 500
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate, "TWITCH_ENABLED", True)
    assert client.get("/_gate/look").json()["emotes"] == {}


@needs_owncast
def test_signing_in_with_twitch_takes_the_viewer_s_chat_color(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert gate.look["colors"] == {"gopnik": "#FF4500"}


@needs_owncast
def test_a_viewer_without_a_chosen_color_keeps_the_hall_one(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows, fake_twitch.color = True, ""
    login(client)
    assert gate.look["colors"] == {}


# --- badges, moderation and bans ---

def admin(path, body=None):
    """Request to the real Owncast admin API."""
    basic = base64.b64encode(f"admin:{OWNCAST_PASSWORD}".encode()).decode()
    req = urllib.request.Request(f"{OWNCAST}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Basic " + basic, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read() or b"null")


def test_badges_come_with_twitch_lines_and_only_used_images_go_out(client, gate, fake_twitch, monkeypatch):
    monkeypatch.setattr(gate, "say_as_twin", lambda name, text: asyncio.sleep(0))
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    monkeypatch.setattr(gate, "TWITCH_ENABLED", True)
    chat(client, json={"body": "Gopnik: привет", "badges": ["moderator/1", "subscriber/0", "кривой", 5]})
    look = client.get("/_gate/look").json()
    assert look["badges"] == {"gopnik": ["moderator/1", "subscriber/0"]}
    # the channel badge takes precedence
    assert look["badgeImages"] == {"moderator/1": ["https://b/mod1", "https://b/mod2", "Moderator"],
                                   "subscriber/0": ["https://b/csub", "https://b/csub2", "Subscriber"]}
    chat(client, json={"body": "Gopnik: снял значки", "badges": []})
    assert gate.look["badges"] == {}


def test_a_banned_viewer_s_twitch_lines_stay_out_of_the_hall(client, gate, monkeypatch):
    said = []
    monkeypatch.setattr(gate, "say_as_twin", lambda name, text: asyncio.sleep(0, said.append(name)))
    gate.bans["tw:gopnik"] = {"login": "gopnik", "oc_ids": ["x"], "at": 0}
    bot(client, "POST")
    assert chat(client, json={"body": "Gopnik: я снова тут"}).json() == {"sent": False, "banned": True}
    assert chat(client, json={"body": "Tolik: а я нет"}).json() == {"sent": True}
    assert said == ["Tolik"]


def test_a_hall_ban_holds_when_the_display_name_differs_from_the_login(client, gate, monkeypatch):
    said = []
    monkeypatch.setattr(gate, "say_as_twin", lambda name, text: asyncio.sleep(0, said.append(name)))
    gate.bans["id:7"] = {"login": "vasya", "name": "vasya", "oc_ids": ["x"], "at": 0}
    bot(client, "POST")
    # the bot sends the display name in the line and the login beside it
    assert chat(client, json={"body": "Вася: я снова тут", "login": "vasya"}).json() == {"sent": False, "banned": True}
    assert chat(client, json={"body": "Вася: без логина"}).json() == {"sent": True}  # an older bot sends none
    assert said == ["Вася"]


def test_a_twin_ban_outlives_the_chat_history(gate, monkeypatch):
    history = [{"user": {"id": "t1", "displayName": "troll [TW]", "disabledAt": "2026-01-01T00:00:00Z"}}]

    async def owncast(method, url, **kw):
        if url.endswith("/api/admin/chat/users/disabled"):
            return 200, []
        if url.endswith("/api/admin/chat/messages"):
            return 200, history
        raise AssertionError(url)
    monkeypatch.setattr(gate, "http", owncast)
    asyncio.run(gate.sync_bans())
    assert gate.bans["tw:troll"]["oc_ids"] == ["t1"]
    history.clear()  # the twin's lines left the history: Owncast no longer shows it at all
    asyncio.run(gate.sync_bans())
    assert "tw:troll" in gate.bans
    history.append({"user": {"id": "t1", "displayName": "troll [TW]"}})  # seen again, enabled in the admin
    asyncio.run(gate.sync_bans())
    assert "tw:troll" not in gate.bans


def test_every_handler_runs_on_the_event_loop(gate):
    # a sync handler runs in a worker thread and races the loop over the session and the state files
    import inspect
    sync = [r.path for r in gate.app.routes
            if hasattr(r, "endpoint") and r.path.startswith(gate.P) and not inspect.iscoroutinefunction(r.endpoint)]
    assert sync == []


def test_signing_in_as_another_account_issues_the_entry_cookie_again(client, fake_twitch, gate, monkeypatch):
    async def identity(user):  # as the real one, without Owncast: the record goes to chatters
        gate.chatters[user["id"]] = {"login": user["login"], "name": user["display_name"], "oc_id": f"oc{user['id']}",
                                     "oc_token": f"tok{user['id']}", "sid": gate.session["sid"]}
        return gate.chatters[user["id"]]
    monkeypatch.setattr(gate, "chat_identity", identity)
    bot(client, "POST")
    fake_twitch.follows = True
    assert handoff_token(login(client)) == "tok7"
    assert client.cookies["kz"].split(".")[2] == "7"
    fake_twitch.user = {"id": "8", "login": "tolik", "display_name": "Tolik"}
    r = twitch_step(client)  # the code is already entered: straight to chat
    assert handoff_token(r) == "tok8"
    assert r.cookies["kz"].split(".")[2] == "8"  # the ban check now reads the account that is chatting
    gate.bans["id:8"] = {"login": "tolik", "name": "Tolik", "oc_ids": ["oc8"], "at": 0}
    assert client.get("/_gate/auth").status_code == 302


def test_an_old_cookie_without_the_account_still_opens_the_hall(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    r = client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    assert r.cookies["kz"].count(".") == 1
    assert client.get("/_gate/auth").status_code == 200


@needs_owncast
def test_a_viewer_banned_in_the_chat_is_out_of_the_hall_until_unbanned(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert client.get("/_gate/auth").status_code == 200
    oc_id = gate.chatters["7"]["oc_id"]
    admin("/api/admin/chat/users/setenabled", {"userId": oc_id, "enabled": False})
    try:
        asyncio.run(gate.sync_bans())
        assert gate.bans["id:7"]["login"] == "gopnik"
        assert json.loads(gate.BANS_FILE.read_text())["id:7"]["oc_ids"] == [oc_id]  # permanent, not per session
        assert client.get("/_gate/auth").status_code == 302  # dropped from the video at once
        assert twitch_step(client).status_code == 403  # and cannot log in again
    finally:
        admin("/api/admin/chat/users/setenabled", {"userId": oc_id, "enabled": True})
    asyncio.run(gate.sync_bans())
    assert "id:7" not in gate.bans  # re-enabled in Owncast: the ban is lifted


@needs_owncast
def test_a_twitch_viewer_banned_in_the_hall_gets_no_fallback_line(client, gate):
    bot(client, "POST")
    mark = f"первая {os.getpid()}-{time.time()}"  # Owncast holds many «Banme [TW]» twins left by other test runs
    assert chat(client, json={"body": f"Banme: {mark}"}).json() == {"sent": True}
    twin_id = next(m["user"]["id"] for m in by(gate, "Banme [TW]") if mark in m["body"])
    admin("/api/admin/chat/users/setenabled", {"userId": twin_id, "enabled": False})
    try:
        after = f"после бана {time.time()}"
        assert chat(client, json={"body": f"Banme: {after}"}).json() == {"sent": False, "banned": True}
        assert not any(after in m["body"] for m in hall_history(gate))  # and none from «Twitch» either
        asyncio.run(gate.sync_bans())
        assert gate.bans["tw:banme"]["oc_ids"] == [twin_id]
    finally:
        admin("/api/admin/chat/users/setenabled", {"userId": twin_id, "enabled": True})
    asyncio.run(gate.sync_bans())
    assert "tw:banme" not in gate.bans  # unbanned in Owncast: lines go through again
    assert chat(client, json={"body": "Banme: снова можно"}).json() == {"sent": True}


def test_only_the_streamer_sees_and_lifts_bans(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "OWNCAST_URL", "http://owncast.invalid")
    gate.bans["tw:banme"] = {"login": "banme", "name": "banme [TW]", "oc_ids": ["x"], "at": 5}
    gate.bans["id:7"] = {"login": "gopnik", "name": "Gopnik", "oc_ids": ["y"], "at": 9}
    bot(client, "POST")
    assert client.get("/_gate/bans").status_code == 403
    assert client.post("/_gate/unban", json={"key": "id:7"}).status_code == 403
    client.cookies.set("kzh", gate.HOST_KEY)  # the permanent dock pass means the streamer
    r = client.get("/_gate/bans")  # Owncast unavailable: the list comes from the file
    assert r.status_code == 200
    assert r.json() == {"bans": [{"key": "id:7", "name": "Gopnik", "twitch": False, "at": 9},
                                 {"key": "tw:banme", "name": "banme", "twitch": True, "at": 5}]}
    assert client.post("/_gate/unban", json={"key": "id:404"}).status_code == 404
    assert client.post("/_gate/unban", json={"key": "id:7"}).status_code == 502  # Owncast did not answer: the ban stays
    assert "id:7" in gate.bans


@needs_owncast
def test_unban_from_the_hall_lets_the_viewer_back_and_lifts_the_ip_ban(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    viewer = client.cookies.get("kz")
    record = gate.chatters["7"]
    # viewer connected to chat: Owncast also bans the connection IP (from docker it is not 127.0.0.1)
    with connect(f"{OWNCAST.replace('http', 'ws')}/ws?accessToken={record['oc_token']}"):
        time.sleep(0.5)
        admin("/api/admin/chat/users/setenabled", {"userId": record["oc_id"], "enabled": False})
        time.sleep(0.5)
    try:
        ip_bans = [b for b in admin("/api/admin/chat/users/ipbans") if b["notes"] == f"Banning of {record['name']}"]
        assert ip_bans, "Owncast не забанил IP – тест его не проверит"
        client.cookies.set("kzh", gate.HOST_KEY)
        listed = client.get("/_gate/bans").json()["bans"]
        row = {k: listed[0][k] for k in ("key", "name", "twitch")}
        assert row == {"key": "id:7", "name": record["name"], "twitch": False}
        assert client.post("/_gate/unban", json={"key": "id:7"}).status_code == 204
        assert "id:7" not in gate.bans and "id:7" not in json.loads(gate.BANS_FILE.read_text())
        assert not [b for b in admin("/api/admin/chat/users/ipbans") if b["notes"] == f"Banning of {record['name']}"]
        assert record["oc_id"] not in {u["id"] for u in admin("/api/admin/chat/users/disabled")}
        asyncio.run(gate.sync_bans())
        assert "id:7" not in gate.bans  # the sync does not restore a lifted ban
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
    oc_id = gate.chatters[CHANNEL_ID]["oc_id"]
    assert oc_id in [u["id"] for u in admin("/api/admin/chat/users/moderators")]
    assert gate.look["badges"]["exitfound"] == ["broadcaster/1"]
    r = client.get(f"/_gate/host?key={gate.HOST_KEY}&next=/embed/chat/readwrite/")
    assert handoff_token(r) == gate.chatters[CHANNEL_ID]["oc_token"]


@needs_owncast
def test_a_viewer_is_not_a_moderator(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert gate.chatters["7"]["oc_id"] not in [u["id"] for u in admin("/api/admin/chat/users/moderators") or []]


# --- who is in chat ---

def test_the_user_list_is_only_for_those_inside(client):
    assert client.get("/_gate/users").status_code == 401


def test_chat_users_are_one_per_person_without_bots_and_with_moderators(gate):
    clients = [
        {"user": {"id": "a", "displayName": "Gopnik", "scopes": ["MODERATOR"]}},
        {"user": {"id": "a", "displayName": "Gopnik"}},                      # second tab
        {"user": {"id": "b", "displayName": "anna"}},
        {"user": {"id": "c", "displayName": "Twitch", "isBot": True}},       # integration
        {"user": None}, "мусор",
    ]
    assert gate.chat_users(clients) == [{"name": "anna", "mod": False}, {"name": "Gopnik", "mod": True}]
    assert gate.chat_users(None) == []


@needs_owncast
def test_inside_the_user_list_comes_from_owncast_and_is_cached(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    calls = []
    real = gate._http
    monkeypatch.setattr(gate, "_http", lambda *a, **kw: calls.append(a[1]) or real(*a, **kw))
    first = client.get("/_gate/users")
    assert first.status_code == 200 and isinstance(first.json()["users"], list)
    client.get("/_gate/users")
    assert sum("/chat/clients" in url for url in calls) == 1  # second request from the cache


def test_without_owncast_the_user_list_says_so_and_does_not_retry_at_once(client, gate, monkeypatch):
    monkeypatch.setattr(gate, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    calls = []

    def down(*a, **kw):
        calls.append(a[1])
        raise gate.Upstream("нет связи")
    monkeypatch.setattr(gate, "_http", down)
    assert client.get("/_gate/users").status_code == 502
    assert client.get("/_gate/users").status_code == 502
    assert len(calls) == 1  # a failure is cached too: the second request does not wait for Owncast
