"""The entry page: its languages, the link preview, the return address, the CSP and its static files."""

import re

import pytest

from helpers import bot, handoff, set_cookie, static


def test_the_login_page_offers_twitch_only_when_configured(client, gate, monkeypatch):
    bot(client, "POST")
    assert "Войти через Twitch" in client.get("/_gate/enter").text
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    assert "Войти через Twitch" not in client.get("/_gate/enter").text
    assert client.get("/_gate/twitch").headers["location"].startswith("/_gate/enter")


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
    rest = rest.replace(gate.pages.PREVIEW_TITLE, "").replace(gate.pages.PREVIEW_SUMMARY, "")
    assert "<title>Cinema</title>" in en and not any("\u0400" <= c <= "\u04ff" for c in rest)
    client.cookies.set("kz_lang", "xx")  # an unknown language falls back to Russian
    assert '<html lang="ru">' in client.get("/_gate/enter").text


def test_without_a_choice_the_browser_language_decides(client, gate):
    bot(client, "POST")
    lang = lambda header: client.get("/_gate/enter", headers={"Accept-Language": header}).text.split('"')[1]  # noqa: E731
    assert lang("en-US,en;q=0.9") == "en"
    assert lang("uk-UA,uk;q=0.9,en;q=0.8") == "ru"  # a CIS language reads Russian
    assert lang("de-DE,ru;q=0.5") == "en"  # the preferred one decides, not any listed
    assert lang("en;q=0.3,ru;q=0.8") == "ru"  # by q, not by order
    assert lang("en;q=0.5,ru;q=0.5") == "en"  # a tie goes to the first listed
    assert lang("") == "ru" and lang("*") == "ru"
    client.cookies.set("kz_lang", "ru")  # an explicit choice beats the browser
    assert lang("en-US") == "ru"


def test_the_language_switch_remembers_the_choice_and_returns_to_the_entry_page(client, gate):
    r = client.get("/_gate/lang?to=en&next=/embed/chat/readwrite/")
    assert r.status_code == 303 and r.headers["location"] == "/_gate/enter?next=/embed/chat/readwrite/"
    assert r.cookies["kz_lang"] == "en"
    header = set_cookie(r, "kz_lang")
    assert "secure" in header and "httponly" not in header  # custom.js reads and sets it too
    unknown = client.get("/_gate/lang?to=xx&next=//evil.example")
    assert unknown.headers["location"] == "/_gate/enter?next=/" and unknown.cookies["kz_lang"] == "ru"


def test_the_entry_page_switches_language_in_place(client, gate):
    bot(client, "POST")
    html = client.get("/_gate/enter").text
    assert 'closest(".lang a")' in static(client, html, "entry.js")
    assert '<meta name="color-scheme" content="dark">' in html
    # The script fetches the switch link. It must end on the entry page with its card, in the new language
    r = client.get("/_gate/lang?to=en&next=/", follow_redirects=True)
    assert r.status_code == 200 and 'class="box"' in r.text and r.text.split('"')[1] == "en"


def test_errors_follow_the_language(client, gate, monkeypatch):
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    client.cookies.set("kz_lang", "en")
    r = client.post("/_gate/enter", data={"code": "nope", "next": "/"})
    assert r.status_code == 401 and "Wrong code." in r.text


def test_the_site_opens_in_the_viewer_s_language(client, gate, monkeypatch):
    # Owncast reads its language only from ?lang=. The gate adds it for English and drops it for Russian
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    bot(client, "POST")
    code = bot(client, "GET").json()["code"]
    client.cookies.set("kz_lang", "en")
    r = client.post("/_gate/enter", data={"code": code, "next": "/?x=1"})
    assert r.headers["location"] == "/?x=1&lang=en"
    client.cookies.set("kz_lang", "ru")
    assert client.get("/_gate/enter?next=/%3Flang%3Den").headers["location"] == "/"
    assert gate.pages.with_lang("/embed/chat/readwrite/", "en") == "/embed/chat/readwrite/?lang=en"


@pytest.mark.parametrize("nxt", ["/\t/evil.example", "/\n/evil.example", "/ /evil.example", "/x\x7f"])
def test_a_control_character_cannot_turn_the_next_page_into_another_site(client, gate, nxt):
    """Browsers and urlparse drop a tab or a newline, so «/<tab>/evil» would become «//evil»."""
    assert gate.security.safe_next(nxt) == "/"
    assert gate.pages.with_lang(gate.security.safe_next(nxt), "en") == "/?lang=en"
    r = client.get("/_gate/lang", params={"to": "en", "next": nxt})
    assert r.headers["location"] == "/_gate/enter?next=/"


def test_the_entry_page_carries_the_link_preview(client, gate):
    """A link-preview crawler has no cookie and lands on the entry page. So the preview card lives there."""
    page_ = client.get("/_gate/enter").text
    assert '<meta property="og:title" content="Кинозал · Cinema">' in page_
    assert (
        '<meta property="og:description" content="Кино со стрима exitfound · Films from the exitfound stream">' in page_
    )


def test_the_handoff_cannot_break_out_of_its_attribute(gate):
    token = 'x"></div><script>alert(1)</script>'
    r = gate.pages.chat_handoff(token, "/", "ru")
    body = r.body.decode()
    assert "<script>alert" not in body and body.count("<script") == 1
    assert handoff(body) == {"token": token, "next": "/"}


def test_gate_pages_run_no_inline_script(client, gate, monkeypatch):
    closed = client.get("/_gate/enter")
    bot(client, "POST")
    pages = [
        closed,
        client.get("/_gate/enter"),
        client.get("/_gate/logout"),
        gate.pages.chat_handoff("token", "/", "ru"),
    ]
    monkeypatch.setattr(gate.config, "TWITCH_ENABLED", False)
    pages.append(client.get("/_gate/enter"))  # the code form
    for r in pages:
        body = r.text if hasattr(r, "text") else r.body.decode()
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", body)), body


def test_the_static_route_serves_only_its_own_files(client):
    page_ = client.get("/_gate/enter").text
    assert "--accent:#BAF" in static(client, page_, "entry.css")
    r = client.get(re.search(r'href="(/_gate/static/[^"]+/entry\.css)"', page_).group(1))
    assert r.headers["content-type"].startswith("text/css")
    for path in (
        "/_gate/static/v/app.py",
        "/_gate/static/v/entry.html",
        "/_gate/static/v/..%2Fapp.py",
        "/_gate/static/v/twitch.svg",
    ):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_handoff_link_without_scripts_is_in_the_viewer_s_language(gate, lang):
    assert gate.texts.TEXT[lang]["to_hall"] in gate.pages.chat_handoff("token", "/", lang).body.decode()


def test_the_site_address_keeps_its_empty_parameters(gate):
    assert gate.pages.with_lang("/?x=&y=1", "en") == "/?x=&y=1&lang=en"
    assert gate.pages.with_lang("/?x=&lang=en", "ru") == "/?x="
