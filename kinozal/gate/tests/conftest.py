"""Fixtures of the gate's tests: the gate under a test environment, its client, Twitch and Owncast chat faked.

Twitch is always faked. Owncast is real when OWNCAST_URL points to a local owncast/owncast:0.3.0 and
OWNCAST_ADMIN_PASSWORD is its admin password; without them the chat tests are skipped.

    docker run -d --name oc-test -p 127.0.0.1:18080:8080 owncast/owncast:0.3.0 -adminpassword testpass
    OWNCAST_URL=http://127.0.0.1:18080 OWNCAST_ADMIN_PASSWORD=testpass python -m pytest
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from helpers import CHANNEL_ID, OWNCAST, OWNCAST_PASSWORD, TOKEN, fresh_gate


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """The gate package, imported fresh under a test environment, its state in tmp_path."""
    monkeypatch.setenv("GATE_SECRET", "s3cret")
    monkeypatch.setenv("GATE_API_TOKEN", TOKEN)
    monkeypatch.setenv("GATE_STATE", str(tmp_path / "session.json"))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("TWITCH_CHANNEL", "exitfound")
    monkeypatch.setenv("GATE_PUBLIC_URL", "https://kz.example.org")
    monkeypatch.setenv("OWNCAST_URL", OWNCAST or "http://owncast.invalid")
    monkeypatch.setenv("OWNCAST_ADMIN_PASSWORD", OWNCAST_PASSWORD)
    return fresh_gate()


@pytest.fixture
def client(gate):
    # TestClient returns a secure cookie only over https
    return TestClient(gate.main.app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture
def fake_twitch(gate, monkeypatch):
    """Fake Twitch with a scripted user, follow and sub. Owncast requests pass through to the real one."""
    real = gate.upstream.http_sync
    tw = type("Twitch", (), {})()
    tw.user = {"id": "7", "login": "gopnik", "display_name": "Gopnik"}
    tw.follows, tw.sub, tw.token_status = False, False, 200
    tw.scopes = ["user:read:follows", "user:read:subscriptions", "user:write:chat"]
    tw.revoked, tw.calls, tw.said = [], [], []
    tw.send = []  # Helix send results in order, as (status, is_sent). Empty means accepted
    tw.refresh_ok, tw.refreshes = True, 0
    tw.color = "#FF4500"  # the viewer's Twitch chat colour. "" means not set
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
                return 200, {
                    "data": [
                        {
                            "set_id": "moderator",
                            "versions": [
                                {
                                    "id": "1",
                                    "title": "Moderator",
                                    "image_url_1x": "https://b/mod1",
                                    "image_url_2x": "https://b/mod2",
                                }
                            ],
                        },
                        {
                            "set_id": "subscriber",
                            "versions": [
                                {
                                    "id": "0",
                                    "title": "Subscriber",
                                    "image_url_1x": "https://b/gsub",
                                    "image_url_2x": "https://b/gsub2",
                                }
                            ],
                        },
                        {"set_id": "vip", "versions": [{"id": "1", "title": "VIP", "image_url_1x": "https://b/vip"}]},
                    ]
                }
            if path == "/helix/chat/badges":
                assert q["broadcaster_id"] == [CHANNEL_ID]
                return 200, {
                    "data": [
                        {
                            "set_id": "subscriber",
                            "versions": [
                                {
                                    "id": "0",
                                    "title": "Subscriber",
                                    "image_url_1x": "https://b/csub",
                                    "image_url_2x": "https://b/csub2",
                                }
                            ],
                        }
                    ]
                }
            raise AssertionError(url)
        if path == "/oauth2/token" and data["grant_type"] == "refresh_token":
            tw.refreshes += 1
            if not tw.refresh_ok:
                return 400, {"message": "Invalid refresh token"}
            return 200, {
                "access_token": f"user-token-{tw.refreshes + 1}",
                "refresh_token": "user-refresh",
                "expires_in": 14000,
            }
        if path == "/oauth2/token":
            assert data["client_secret"] == "csecret" and data["code"] == "good-code"
            if tw.token_status != 200:
                return tw.token_status, None
            return 200, {
                "access_token": "user-token",
                "refresh_token": "user-refresh",
                "expires_in": 14000,
                "scope": tw.scopes,
            }
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

    monkeypatch.setattr(gate.upstream, "http_sync", fake)
    monkeypatch.setattr(gate.twitch, "revoke_later", lambda tokens: tw.revoked.extend(tokens))
    return tw


@pytest.fixture
def fake_chat(gate, monkeypatch):
    """Fake Owncast chat registration. Each Twitch viewer gets their own chat user."""

    async def identity(user):
        record = {
            "login": user["login"],
            "name": user["display_name"],
            "oc_id": f"oc-{user['id']}",
            "oc_token": f"oc-token-{user['id']}",
            "sid": gate.state.session["sid"] if gate.state.session else None,
        }
        gate.state.chatters[user["id"]] = record
        return record

    monkeypatch.setattr(gate.owncast, "chat_identity", identity)
