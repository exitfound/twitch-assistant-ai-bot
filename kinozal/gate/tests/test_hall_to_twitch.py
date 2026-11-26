"""Hall lines to Twitch as the viewer: the hall loop, the nick reset and the viewers' voices."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from helpers import (
    CHANNEL_ID,
    anonymous_token,
    bot,
    can_watch,
    hall_history,
    handoff_token,
    login,
    needs_owncast,
    needs_stream,
    say,
    static,
    twitch_step,
)


def test_the_hall_loop_survives_failures_and_logs_an_outage_once(client, gate, monkeypatch, capsys):
    """The loop logs an Owncast outage once and goes on after a bad line.

    Bans sync on a session's first round, then once per BAN_SYNC_SECONDS."""
    bot(client, "POST")
    monkeypatch.setattr(gate.config, "HALL_POLL_SECONDS", 0)
    monkeypatch.setattr(gate.bridge, "BAN_SYNC_SECONDS", 3600)
    rounds = [
        gate.upstream.Upstream("owncast down"),
        gate.upstream.Upstream("owncast down"),
        ValueError("bad line"),
        None,
        gate.upstream.Upstream("down again"),
        asyncio.CancelledError(),
    ]
    synced = []

    async def relay():
        result = rounds.pop(0)
        if result is not None:
            raise result

    async def sync():
        synced.append(True)

    monkeypatch.setattr(gate.bridge, "relay_once", relay)
    monkeypatch.setattr(gate.moderation, "sync_bans", sync)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gate.bridge.hall_loop())
    out = capsys.readouterr().out
    assert out.count("owncast down") == 1 and "bad line" in out and "down again" in out
    assert synced == [True]


def test_the_hall_loop_survives_any_failure_of_the_ban_sync_and_the_lease(client, gate, monkeypatch, capsys):
    """Any failure of the ban sync or the lease check is logged, not fatal. Only this loop relays the hall."""
    bot(client, "POST")
    monkeypatch.setattr(gate.config, "HALL_POLL_SECONDS", 0)
    monkeypatch.setattr(gate.bridge, "BAN_SYNC_SECONDS", 0)
    syncs = [KeyError("oc_ids"), gate.upstream.Upstream("owncast down")]
    relayed = []

    async def sync():
        if syncs:
            raise syncs.pop(0)

    async def relay():
        relayed.append(True)
        if len(relayed) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(gate.moderation, "sync_bans", sync)
    monkeypatch.setattr(gate.bridge, "relay_once", relay)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gate.bridge.hall_loop())
    out = capsys.readouterr().out
    assert "KeyError" in out and "owncast down" in out and len(relayed) == 3


def test_an_unreachable_chat_websocket_is_an_upstream_failure(gate, monkeypatch, capsys):
    monkeypatch.setattr(gate.config, "OWNCAST_URL", "http://127.0.0.1:9")
    with pytest.raises(gate.upstream.Upstream):
        gate.owncast.set_name("token", "Gopnik")
    gate.state.chatters["7"] = {"login": "gopnik", "name": "Gopnik", "oc_id": "oc7", "oc_token": "t"}
    asyncio.run(gate.bridge.keep_name({"id": "oc7", "displayName": "root"}))  # logged, the hall loop goes on
    assert "nick Gopnik not set back" in capsys.readouterr().out


@needs_owncast
@needs_stream
def test_a_renamed_viewer_gets_the_twitch_name_back(client, fake_twitch, gate, capsys):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.bridge.relay_once())
    oc_id = gate.state.chatters["7"]["oc_id"]

    def name_now():  # history returns the author's current nick
        return [m["user"]["displayName"] for m in hall_history(gate) if m["user"]["id"] == oc_id][-1]

    say(token, "I am root now", rename="root")
    assert name_now() == "root"
    asyncio.run(gate.bridge.relay_once())
    assert "hall nick «root» set back to Gopnik" in capsys.readouterr().out
    assert name_now() == "Gopnik"
    asyncio.run(gate.bridge.relay_once())  # already Gopnik, so left alone
    assert "set back" not in capsys.readouterr().out


@needs_owncast
@needs_stream
def test_hall_lines_go_to_twitch_as_the_viewer(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    say(token, "said before the gate")
    asyncio.run(gate.bridge.relay_once())  # the first pass of a session does not replay older lines
    assert fake_twitch.said == []
    say(token, "hello from the hall", rename="exitfound")  # renamed, still the same viewer
    say(anonymous_token(), "in by the code")  # no Twitch login, so not sent to Twitch
    asyncio.run(gate.bridge.relay_once())
    asyncio.run(gate.bridge.relay_once())  # a repeated pass sends no duplicates
    assert fake_twitch.said == [
        (
            "Bearer user-token",
            {"broadcaster_id": CHANNEL_ID, "sender_id": "7", "message": "🎬 [KZ] hello from the hall"},
        )
    ]


@needs_owncast
@needs_stream
def test_a_dead_access_token_is_refreshed_once(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.bridge.relay_once())
    fake_twitch.send = [(401, False)]
    say(token, "after the expiry")
    asyncio.run(gate.bridge.relay_once())
    assert [auth for auth, _ in fake_twitch.said] == ["Bearer user-token", "Bearer user-token-2"]
    assert fake_twitch.refreshes == 1


@needs_owncast
@needs_stream
def test_a_token_twitch_no_longer_takes_drops_the_voice(client, fake_twitch, gate, capsys):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.bridge.relay_once())
    fake_twitch.send, fake_twitch.refresh_ok = [(401, False)], False
    say(token, "one")
    asyncio.run(gate.bridge.relay_once())
    assert gate.state.voices == {}
    assert "voice dropped" in capsys.readouterr().out


@needs_owncast
@needs_stream
def test_a_line_twitch_refuses_is_only_logged(client, fake_twitch, gate, capsys):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    asyncio.run(gate.bridge.relay_once())
    fake_twitch.send = [(200, False)]
    say(token, "bad one")
    asyncio.run(gate.bridge.relay_once())
    assert "AutoMod" in capsys.readouterr().out
    assert gate.state.voices  # the voice stays


@needs_owncast
@pytest.mark.parametrize("end", ["stop", "new session", "lease"])
def test_the_session_end_forgets_and_revokes_the_voices(client, fake_twitch, gate, monkeypatch, end):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    assert gate.state.voices and fake_twitch.revoked == []
    if end == "stop":
        bot(client, "DELETE")
    elif end == "new session":
        bot(client, "POST")
    else:
        gate.state.session["seen"] -= gate.config.LEASE + 1
        gate.session.expire()
    assert gate.state.voices == {}
    assert fake_twitch.revoked == ["user-token"]


@pytest.mark.parametrize("end", ["stop", "new session"])
def test_a_session_ending_during_the_sign_in_keeps_no_voice(client, fake_twitch, fake_chat, gate, monkeypatch, end):
    """A session closed while the sign-in waits shows the closed page and keeps no token.

    A token kept past forget_voices() would never be revoked."""
    bot(client, "POST")
    fake_twitch.follows = True
    real = gate.twitch.may_watch

    async def watch(*args):
        # Same as DELETE and POST /api/session. The client cannot be called from inside the app
        gate.state.session = None if end == "stop" else {**gate.state.session, "sid": "next-session"}
        gate.voices.forget_voices()
        return await real(*args)

    monkeypatch.setattr(gate.twitch, "may_watch", watch)
    r = twitch_step(client)
    assert r.status_code == 200 and gate.texts.TEXT["ru"]["closed_lead"] in r.text
    assert not gate.state.voices and "user-token" in fake_twitch.revoked


@needs_owncast
def test_without_the_write_right_the_token_is_revoked_at_once(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    fake_twitch.scopes = ["user:read:follows", "user:read:subscriptions"]
    assert login(client).status_code == 200 and can_watch(client)
    assert gate.state.voices == {}
    assert fake_twitch.revoked == ["user-token"]


@needs_owncast
def test_a_second_login_replaces_the_voice(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    login(client)
    login(client)
    assert len(gate.state.voices) == 1
    assert fake_twitch.revoked == ["user-token"]  # the same viewer's previous token


@needs_owncast
def test_logout_drops_the_cookie_and_revokes_the_viewer_s_twitch_token(client, fake_twitch, gate):
    bot(client, "POST")
    fake_twitch.follows = True
    token = handoff_token(login(client))
    assert can_watch(client) and gate.state.voices
    page_ = client.get("/_gate/logout")
    assert "/logout.js" in page_.text and 'removeItem("accessToken")' in static(client, page_.text, "logout.js")
    assert not can_watch(client)
    assert client.post("/_gate/logout", json={"token": "чужой"}).status_code == 204
    assert gate.state.voices and fake_twitch.revoked == []
    assert client.post("/_gate/logout", json={"token": token}).status_code == 204
    assert gate.state.voices == {} and fake_twitch.revoked == ["user-token"]


def test_a_renamed_nick_is_set_back_at_most_once_per_rename_window(gate, monkeypatch):
    """keep_name() resets a hall nick that differs from the Twitch one, and waits RENAME_EVERY before the next try."""
    clock, renamed = [1000.0], []
    monkeypatch.setattr(gate.bridge, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(gate.owncast, "set_name", lambda token, name: renamed.append((token, name)))
    gate.state.chatters["7"] = {"login": "gopnik", "name": "Gopnik", "oc_id": "oc7", "oc_token": "t7"}

    def seen(display_name):
        asyncio.run(gate.bridge.keep_name({"id": "oc7", "displayName": display_name}))

    seen("Gopnik")  # already the Twitch nick
    seen("")  # no nick in the line
    asyncio.run(gate.bridge.keep_name({"id": "stranger", "displayName": "root"}))  # not a Twitch viewer
    assert renamed == []
    seen("root")
    assert renamed == [("t7", "Gopnik")]
    clock[0] += gate.bridge.RENAME_EVERY - 1
    seen("root")
    assert len(renamed) == 1
    clock[0] += 2
    seen("root")
    assert len(renamed) == 2


def test_a_viewer_s_new_chat_user_takes_over_the_voice_of_the_old_one(gate, monkeypatch):
    """One voice per Twitch account: a sign-in under a new chat user drops and revokes the old one, others stay."""
    revoked = []
    monkeypatch.setattr(gate.twitch, "revoke_later", revoked.extend)
    gopnik, tolik = {"id": "7", "login": "gopnik"}, {"id": "8", "login": "tolik"}

    def tok(n):
        return {"access_token": f"a{n}", "refresh_token": f"r{n}", "expires_in": 3600}

    gate.voices.remember("oc-old", gopnik, tok(1))
    gate.voices.remember("oc-tolik", tolik, tok(2))
    gate.voices.remember("oc-new", gopnik, tok(3))
    assert sorted(gate.state.voices) == ["oc-new", "oc-tolik"]
    assert gate.state.voices["oc-new"]["access"] == "a3" and revoked == ["a1"]


def test_the_relay_skips_the_first_pass_bots_and_lines_that_are_not_chat(gate, monkeypatch):
    """Only new, visible CHAT lines of a viewer with a voice go to Twitch; nick resets look at viewers only."""
    sent, checked = [], []
    lines = [{"id": "old", "type": "CHAT", "body": "said before", "user": {"id": "oc7"}}]

    async def history(method, path, json_body=None):
        return list(lines)

    async def say(oc_id, text):
        sent.append((oc_id, text))

    async def keep(user):
        checked.append(user.get("id"))

    monkeypatch.setattr(gate.owncast, "integration", history)
    monkeypatch.setattr(gate.voices, "say", say)
    monkeypatch.setattr(gate.bridge, "keep_name", keep)
    gate.session.start(None)
    gate.state.voices["oc7"] = {"twitch_id": "7"}
    asyncio.run(gate.bridge.relay_once())  # the first pass of a session only records what was said
    assert sent == [] and checked == []
    lines += [
        {"id": "a", "type": "CHAT", "body": "<p>hello</p>", "user": {"id": "oc7"}},
        {"id": "b", "type": "CHAT", "body": "hidden", "visible": False, "user": {"id": "oc7"}},
        {"id": "c", "type": "NAME_CHANGE", "body": "renamed", "user": {"id": "oc7"}},
        {"id": "d", "type": "CHAT", "body": "from a bot", "user": {"id": "bot", "isBot": True}},
        {"id": "e", "type": "CHAT", "body": "no voice", "user": {"id": "oc8"}},
        {"id": "f", "type": "CHAT", "body": "", "user": {"id": "oc7"}},
    ]
    asyncio.run(gate.bridge.relay_once())
    asyncio.run(gate.bridge.relay_once())  # a repeated pass sends nothing twice
    assert sent == [("oc7", "hello")]
    assert checked == ["oc7", "oc7", "oc7", "oc8", "oc7"]


def test_a_line_that_fails_to_reach_twitch_does_not_lose_the_lines_after_it(gate, monkeypatch):
    """A failed send ends the pass, and the next pass relays the rest of the batch, or those lines would be lost."""
    sent, fail = [], {"a"}
    lines = [{"id": "x", "type": "CHAT", "body": "before", "user": {"id": "oc7"}}]

    async def history(method, path, json_body=None):
        return list(lines)

    async def say(oc_id, text):
        if text in fail:
            fail.discard(text)
            raise gate.upstream.Upstream("twitch timed out")
        sent.append(text)

    async def keep(user):
        pass

    monkeypatch.setattr(gate.owncast, "integration", history)
    monkeypatch.setattr(gate.voices, "say", say)
    monkeypatch.setattr(gate.bridge, "keep_name", keep)
    gate.session.start(None)
    gate.state.voices["oc7"] = {"twitch_id": "7"}
    asyncio.run(gate.bridge.relay_once())
    lines += ["not a line", *({"id": t, "type": "CHAT", "body": t, "user": {"id": "oc7"}} for t in ("a", "b", "c"))]
    with pytest.raises(gate.upstream.Upstream):
        asyncio.run(gate.bridge.relay_once())
    asyncio.run(gate.bridge.relay_once())
    assert sent == ["b", "c"]


def test_the_relay_remembers_only_the_latest_lines(gate, monkeypatch):
    monkeypatch.setattr(gate.bridge, "HALL_SEEN_KEEP", 2)

    async def history(method, path, json_body=None):
        return [{"id": i, "type": "CHAT", "user": {}} for i in ("a", "b", "c")]

    monkeypatch.setattr(gate.owncast, "integration", history)
    gate.session.start(None)
    asyncio.run(gate.bridge.relay_once())
    assert list(gate.bridge.hall["seen"]) == ["b", "c"]


def voice(gate, expires_in: float = 3600) -> None:
    """Gopnik's voice under the chat user oc7, the channel id known, as after a sign-in."""
    gate.twitch.channel_id = CHANNEL_ID
    gate.state.voices["oc7"] = {
        "twitch_id": "7",
        "login": "gopnik",
        "access": "user-token",
        "refresh": "user-refresh",
        "expires": time.time() + expires_in,
    }


def test_a_hall_line_goes_to_twitch_with_the_mark_and_within_twitch_s_limit(gate, fake_twitch):
    voice(gate)
    asyncio.run(gate.voices.say("oc7", "x" * 600))
    assert fake_twitch.said == [
        (
            "Bearer user-token",
            {"broadcaster_id": CHANNEL_ID, "sender_id": "7", "message": ("🎬 [KZ] " + "x" * 600)[:500]},
        )
    ]


def test_without_a_voice_or_the_channel_id_nothing_goes_to_twitch(gate, fake_twitch):
    asyncio.run(gate.voices.say("oc7", "hello"))  # no voice
    voice(gate)
    gate.twitch.channel_id = None
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert fake_twitch.said == []


def test_a_token_close_to_expiry_is_refreshed_first(gate, fake_twitch):
    voice(gate, expires_in=60)
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert fake_twitch.refreshes == 1 and fake_twitch.said[0][0] == "Bearer user-token-2"


def test_a_token_that_cannot_be_refreshed_drops_the_voice(gate, fake_twitch, capsys):
    voice(gate, expires_in=60)
    fake_twitch.refresh_ok = False
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert gate.state.voices == {} and fake_twitch.said == []
    assert "not refreshed, voice dropped" in capsys.readouterr().out


def test_a_401_refreshes_the_token_once_and_sends_again(gate, fake_twitch):
    voice(gate)
    fake_twitch.send = [(401, False)]
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert [auth for auth, _ in fake_twitch.said] == ["Bearer user-token", "Bearer user-token-2"]
    assert "oc7" in gate.state.voices


@pytest.mark.parametrize("refresh_ok", [True, False])
def test_a_token_twitch_keeps_refusing_drops_the_voice(gate, fake_twitch, capsys, refresh_ok):
    voice(gate)
    fake_twitch.send, fake_twitch.refresh_ok = [(401, False), (401, False)], refresh_ok
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert gate.state.voices == {} and fake_twitch.refreshes == 1
    assert "refuses the token of gopnik, voice dropped" in capsys.readouterr().out


@pytest.mark.parametrize("answer", [(200, False), (500, True)])
def test_a_line_twitch_does_not_post_is_logged_and_the_voice_stays(gate, fake_twitch, capsys, answer):
    voice(gate)
    fake_twitch.send = [answer]
    asyncio.run(gate.voices.say("oc7", "hello"))
    out = capsys.readouterr().out
    assert f"not accepted (HTTP {answer[0]}" in out and "oc7" in gate.state.voices


@pytest.mark.parametrize("answer", [(200, None), (200, {}), (500, {"access_token": "new"})])
def test_an_unusable_refresh_answer_drops_the_voice(gate, fake_twitch, monkeypatch, capsys, answer):
    voice(gate, expires_in=60)
    real = gate.upstream.http_sync

    def refresh(method, url, **kw):
        if url.endswith("/oauth2/token") and kw["data"]["grant_type"] == "refresh_token":
            return answer
        return real(method, url, **kw)

    monkeypatch.setattr(gate.upstream, "http_sync", refresh)
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert gate.state.voices == {} and fake_twitch.said == []


@pytest.mark.parametrize(
    "answer, logged",
    [
        ((200, {"data": []}), "HTTP 200, no reason given"),
        ((200, {"data": [{"is_sent": False}]}), "HTTP 200, no reason given"),
        ((200, {"data": [{"is_sent": False, "drop_reason": {"message": "AutoMod"}}]}), "HTTP 200, AutoMod"),
        ((400, None), "HTTP 400, no reason given"),
    ],
)
def test_twitch_s_reason_for_not_posting_is_logged(gate, fake_twitch, monkeypatch, capsys, answer, logged):
    voice(gate)
    real = gate.upstream.http_sync
    monkeypatch.setattr(
        gate.upstream,
        "http_sync",
        lambda method, url, **kw: answer if url.endswith("/helix/chat/messages") else real(method, url, **kw),
    )
    asyncio.run(gate.voices.say("oc7", "hello"))
    assert f"not accepted ({logged})" in capsys.readouterr().out
