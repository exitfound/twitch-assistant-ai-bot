"""The code entry, the session and its lease, the brute-force limits, Caddy's check and the dock pass."""

import json
import time

from fastapi.testclient import TestClient

from helpers import bot, can_watch, client_address, fresh_gate, handoff_token, set_cookie, twitch_step


def test_code_entry_and_a_new_session_invalidating_it(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)  # no Twitch app, so the code alone lets in
    code = bot(client, "POST", json={"title": "Alien"}).json()["code"]
    assert client.get("/_gate/auth").status_code == 302
    assert client.post("/_gate/enter", data={"code": "x", "next": "/"}).status_code == 401
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 303
    assert can_watch(client)
    bot(client, "POST")
    assert not can_watch(client)


def test_wrong_codes_lock_the_address_out_for_the_window(client, gate, monkeypatch):
    """After FAIL_LIMIT wrong codes an address waits FAIL_WINDOW, even with the right code.

    A 4-digit code has only 10 000 values. Other addresses are not affected."""
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    code = bot(client, "POST").json()["code"]
    wrong = "0000" if code != "0000" else "1111"
    for _ in range(gate.security.FAIL_LIMIT):
        assert client.post("/_gate/enter", data={"code": wrong, "next": "/"}).status_code == 401
    locked = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert locked.status_code == 429 and not can_watch(client)
    other = {"x-forwarded-for": "5.6.7.8"}
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}, headers=other).status_code == 303
    monkeypatch.setattr(gate.config, "LEASE", 0)  # the clock jump below must not close the session
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + gate.security.FAIL_WINDOW + 1)
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 303


def test_wrong_host_keys_lock_the_address_out_too(client, gate):
    for i in range(gate.security.FAIL_LIMIT):
        assert client.get(f"/_gate/host?key=wrong{i}").status_code == 404
    assert client.get(f"/_gate/host?key={gate.security.HOST_KEY}").status_code == 429


def test_wrong_codes_count_per_twitch_account_behind_a_shared_address(client, gate, fake_twitch, fake_chat):
    """Wrong codes lock out the Twitch account, not the shared address.

    Viewers behind one carrier NAT share an address. It closes only past FAIL_IP_LIMIT."""
    code = bot(client, "POST").json()["code"]
    wrong = "0000" if code != "0000" else "1111"
    fake_twitch.follows = True
    twitch_step(client)
    for _ in range(gate.security.FAIL_LIMIT):
        assert client.post("/_gate/enter", data={"code": wrong, "next": "/"}).status_code == 401
    assert client.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 429
    other = TestClient(gate.main.app, base_url="https://testserver", follow_redirects=False)
    fake_twitch.user = {"id": "8", "login": "vasya", "display_name": "Vasya"}
    twitch_step(other)
    assert other.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 200
    gate.security.failures[client_address(gate)] = [time.time()] * gate.security.FAIL_IP_LIMIT
    third = TestClient(gate.main.app, base_url="https://testserver", follow_redirects=False)
    fake_twitch.user = {"id": "9", "login": "petya", "display_name": "Petya"}
    twitch_step(third)
    assert third.post("/_gate/enter", data={"code": code, "next": "/"}).status_code == 429


def test_non_ascii_secrets_are_refused_not_crashed_on(client, gate, monkeypatch):
    """compare_digest raises on a non-ASCII str. A forged cookie or code must get a refusal, not a 500."""
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    assert not gate.security.same("é", "e") and gate.security.same("abc", "abc")
    assert client.post("/_gate/enter", data={"code": "١٢٣٤", "next": "/"}).status_code == 401
    sid = gate.state.session["sid"]
    for forged in ("é.é", f"{sid}.é", f"{sid}.{gate.security.sign(sid)}.é.é"):
        assert client.get("/_gate/auth", headers={"cookie": f"kz={forged}".encode()}).status_code == 302


def test_the_last_forwarded_address_is_the_client(gate):
    """Caddy has no trusted proxies and appends the real address last. Anything the client sent is ignored."""
    req = type("Req", (), {"headers": {"x-forwarded-for": "1.2.3.4, 9.9.9.9"}, "client": None})()
    assert gate.security.client_ip(req) == "9.9.9.9"


def test_a_state_file_is_replaced_whole_and_a_broken_one_reads_as_default(gate, tmp_path):
    path = tmp_path / "state.json"
    assert gate.storage.read_json(path, {"empty": True}) == {"empty": True}
    gate.storage.write_json(path, {"ник": "Гопник"})
    assert "Гопник" in path.read_text()  # readable on the host as it is
    assert gate.storage.read_json(path, None) == {"ник": "Гопник"} and not path.with_suffix(".tmp").exists()
    path.write_text("{not json")
    assert gate.storage.read_json(path, []) == []


def test_delete_says_whether_there_was_a_session(client):
    assert bot(client, "DELETE").json()["closed"] is False
    bot(client, "POST")
    assert bot(client, "DELETE").json()["closed"] is True


def test_a_code_without_twitch_is_refused(client):
    code = bot(client, "POST").json()["code"]
    page_ = client.get("/_gate/enter")
    assert "Войти через Twitch" in page_.text and 'name="code"' not in page_.text  # no code field yet
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 401 and "Сначала войди через Twitch" in r.text and not can_watch(client)


def test_without_a_twitch_app_the_code_alone_lets_in(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    code = bot(client, "POST").json()["code"]
    assert 'name="code"' in client.get("/_gate/enter").text
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 303 and can_watch(client)


def test_the_live_counter_knows_the_session_start_only_for_viewers(client, gate, monkeypatch):
    assert client.get("/_gate/live").status_code == 401
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    started = bot(client, "POST", json={"title": "Stalker"}).json()["started"]
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    r = client.get("/_gate/live").json()
    assert r["started"] == started and abs(r["now"] - started) < 5
    assert r["title"] == "Stalker"
    bot(client, "PATCH", json={"title": "Solaris"})
    assert client.get("/_gate/live").json()["title"] == "Solaris"  # !film title, shown below the player
    bot(client, "DELETE")
    assert client.get("/_gate/live").status_code == 401  # the session ended, so the cookie no longer lets in


def test_the_obs_placeholder_sees_the_film_without_signing_in(client):
    r = client.get("/_gate/now")
    assert r.json()["open"] is False and r.headers["access-control-allow-origin"] == "*"
    started = bot(client, "POST", json={"title": "Stalker"}).json()["started"]
    r = client.get("/_gate/now").json()
    assert r == {"open": True, "title": "Stalker", "started": started, "now": r["now"]}
    assert "code" not in r
    bot(client, "DELETE")
    r = client.get("/_gate/now").json()
    assert (r["open"], r["title"], r["started"]) == (False, None, None)


def test_a_viewer_without_twitch_has_no_voice_to_lose(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    assert client.get("/_gate/live").json()["voice"] is None


def test_every_handler_runs_on_the_event_loop(gate):
    import inspect

    sync = [
        r.path
        for r in gate.main.app.routes
        if hasattr(r, "endpoint") and r.path.startswith(gate.config.P) and not inspect.iscoroutinefunction(r.endpoint)
    ]
    assert sync == []


def test_an_old_cookie_without_the_account_still_opens_the_hall(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    r = client.post("/_gate/enter", data={"code": bot(client, "GET").json()["code"], "next": "/"})
    assert r.cookies["kz"].count(".") == 1
    assert client.get("/_gate/auth").status_code == 200


def test_a_forged_twitch_step_cookie_is_refused(client, gate, fake_twitch, fake_chat):
    """kzp names a Twitch account only with its signature, or anyone could take another viewer's chat name."""
    code = bot(client, "POST").json()["code"]
    fake_twitch.follows = True
    twitch_step(client)
    assert "Twitch: <b>Gopnik</b>" in client.get("/_gate/enter").text
    client.cookies.clear()
    client.cookies.set("kzp", "7.forged", path="/_gate")
    assert "Twitch:" not in client.get("/_gate/enter").text
    r = client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert r.status_code == 401 and not can_watch(client)


def test_the_entry_cookies_are_secure_and_out_of_reach_of_scripts(client, gate, fake_twitch, fake_chat):
    """kz, kzp and kzh are Secure, HttpOnly and SameSite=Lax. Logout clears kz and kzp with the same attributes."""
    code = bot(client, "POST").json()["code"]
    fake_twitch.follows = True
    pending = set_cookie(twitch_step(client), "kzp")
    entry = set_cookie(client.post("/_gate/enter", data={"code": code, "next": "/"}), "kz")
    host = set_cookie(client.get(f"/_gate/host?key={gate.security.HOST_KEY}"), "kzh")
    for header in (pending, entry, host):
        assert "secure" in header and "httponly" in header and "samesite=lax" in header, header
    out = client.get("/_gate/logout")
    for name in ("kz", "kzp"):
        header = set_cookie(out, name)
        assert "max-age=0" in header and "secure" in header and "httponly" in header, header


def test_a_gate_restart_keeps_the_session_its_code_and_the_viewers_inside(client, gate, monkeypatch):
    """The session, the chatters and the bans are read back from /data at start, so a restart lets nobody out."""
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    opened = bot(client, "POST", json={"title": "Alien"}).json()
    client.post("/_gate/enter", data={"code": opened["code"], "next": "/"})
    gate.state.bans["id:9"] = {"login": "troll", "name": "troll", "oc_ids": ["x"], "at": 1}
    gate.state.save_bans()
    restarted = fresh_gate()
    again = TestClient(
        restarted.main.app, base_url="https://testserver", follow_redirects=False, cookies=client.cookies
    )
    assert can_watch(again) and "id:9" in restarted.state.bans
    assert bot(again, "GET").json() == opened


def test_a_session_file_without_the_lease_time_gets_one_at_start(client, gate):
    """An older session file has no "seen". The start sets it to now, or the lease check would fail on it."""
    opened = bot(client, "POST").json()
    saved = json.loads(gate.config.STATE_FILE.read_text())
    del saved["seen"]
    gate.config.STATE_FILE.write_text(json.dumps(saved))
    restarted = fresh_gate()
    again = TestClient(restarted.main.app, base_url="https://testserver", follow_redirects=False)
    assert abs(restarted.state.session["seen"] - time.time()) < 5
    assert bot(again, "GET").json()["code"] == opened["code"]


def test_without_a_session_the_entry_page_says_the_hall_is_closed(client, gate):
    page = client.get("/_gate/enter").text
    assert gate.texts.TEXT["ru"]["closed_lead"] in page and 'name="code"' not in page


def test_a_viewer_signed_in_with_twitch_has_a_voice_until_the_gate_forgets_it(client, gate, fake_twitch, fake_chat):
    """/live tells custom.js whether hall lines still go to Twitch, so it can offer «Reconnect»."""
    code = bot(client, "POST").json()["code"]
    fake_twitch.follows = True
    twitch_step(client)
    client.post("/_gate/enter", data={"code": code, "next": "/"})
    assert client.get("/_gate/live").json()["voice"] is True
    gate.state.voices.clear()
    assert client.get("/_gate/live").json()["voice"] is False


def test_the_dock_pass_with_an_old_viewer_cookie_has_no_voice(client, gate, fake_twitch, fake_chat):
    """With the dock's pass, an entry cookie of a closed session names nobody, so /live reports no voice."""
    code = bot(client, "POST").json()["code"]
    fake_twitch.follows = True
    twitch_step(client)
    client.post("/_gate/enter", data={"code": code, "next": "/"})
    bot(client, "POST")  # a new session: the old kz no longer counts
    client.cookies.set("kzh", gate.security.HOST_KEY)
    assert client.get("/_gate/live").json()["voice"] is None


def test_the_dock_pass_writes_as_the_streamer_once_the_streamer_signed_in(client, gate):
    """/host hands the streamer's chat token to the dock, not the first chatter's."""
    gate.state.chatters["7"] = {"login": "gopnik", "name": "Gopnik", "oc_id": "oc7", "oc_token": "viewer-token"}
    gate.state.chatters["100"] = {
        "login": "ExitFound",
        "name": "ExitFound",
        "oc_id": "oc1",
        "oc_token": "streamer-token",
    }
    r = client.get(f"/_gate/host?key={gate.security.HOST_KEY}&next=/embed/chat/readwrite/")
    assert handoff_token(r) == "streamer-token"


def test_signing_in_again_after_the_code_clears_the_twitch_step_cookie_the_same_way(
    client, gate, fake_twitch, fake_chat
):
    code = bot(client, "POST").json()["code"]
    fake_twitch.follows = True
    twitch_step(client)
    client.post("/_gate/enter", data={"code": code, "next": "/"})
    header = set_cookie(twitch_step(client), "kzp")
    assert "max-age=0" in header and "secure" in header and "httponly" in header
