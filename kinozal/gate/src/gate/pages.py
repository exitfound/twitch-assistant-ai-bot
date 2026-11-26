"""The gate's own pages: the entry page, the chat handoff, their static files and CSP, the viewer's language."""

import hashlib
import html
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import Request
from fastapi.responses import HTMLResponse

from gate import config, security, state, texts

WEB = Path(__file__).with_name("web")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
# Read at start, served by name only. The hash in the address changes with the content
STATIC = {f.name: (f.read_bytes(), STATIC_TYPES[f.suffix]) for f in sorted(WEB.iterdir()) if f.suffix in STATIC_TYPES}
STATIC_URL = f"{config.P}/static/" + hashlib.sha256(b"".join(body for body, _ in STATIC.values())).hexdigest()[:12]
ENTRY = (WEB / "entry.html").read_text().replace("{{STATIC}}", STATIC_URL)
TWITCH_ICON = (WEB / "twitch.svg").read_text().strip()
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self'; connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
LANG_NAMES = {"ru": "Русский", "en": "English"}

# Link preview for Discord and Telegram. A crawler has no cookie and lands on the entry page,
# so it carries the same bilingual card that kinozal.sh setup gives Owncast
PREVIEW_TITLE = f"{texts.TEXT['ru']['title']} · {texts.TEXT['en']['title']}"
PREVIEW_SUMMARY = f"Кино со стрима {config.TWITCH_CHANNEL} · Films from the {config.TWITCH_CHANNEL} stream"


def lang_of(req: Request) -> str:
    """The viewer's choice (kz_lang), else the browser's first language."""
    lang = req.cookies.get(config.LANG_COOKIE, "")
    return lang if lang in texts.LANGS else texts.browser_lang(req.headers.get("accept-language", ""))


def with_lang(nxt: str, lang: str) -> str:
    """The site's address in the viewer's language. Owncast reads it only from ?lang=, else from the browser."""
    u = urlparse(nxt)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "lang"]
    if lang == "en":
        query.append(("lang", "en"))
    return security.safe_next(urlunparse(u._replace(query=urlencode(query))))


def html_page(markup: str, status: int = 200) -> HTMLResponse:
    """A gate page: never cached, under a CSP that runs no inline script."""
    return HTMLResponse(
        markup, status_code=status, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP}
    )


def chat_handoff(oc_token: str, nxt: str, lang: str) -> HTMLResponse:
    """Put the Owncast chat token into localStorage for Owncast's web client, then open the site (web/handoff.js)."""
    body = (
        f'<div id="kz-handoff" data-token="{html.escape(oc_token)}" data-next="{html.escape(nxt)}" hidden></div>'
        f'<script src="{STATIC_URL}/handoff.js"></script>'
        f'<noscript><a href="{html.escape(nxt)}">{html.escape(texts.tr(lang, "to_hall"))}</a></noscript>'
    )
    return html_page(f'<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">{body}')


def page(
    lang: str, nxt: str = "/", error: str = "", closed: bool = False, status: int = 200, who: str | None = None
) -> HTMLResponse:
    """The entry page: closed, the Twitch button, or the code form (after Twitch, or without a Twitch app)."""

    def t(key: str) -> str:
        return html.escape(texts.tr(lang, key))

    if closed:
        body = f"""
      <p class="lead">{t("closed_lead")}</p>
      <p class="hint">{t("closed_hint")}</p>"""
    else:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        title = state.session.get("title") if state.session else None
        now = f'<p class="now"><span>{t("now")}</span> {html.escape(title)}</p>' if title else ""
        form = f"""
      <form method="post" action="{config.P}/enter" autocomplete="off">
        <input type="hidden" name="next" value="{html.escape(nxt)}">
        <input class="code" name="code" inputmode="numeric" pattern="[0-9]*"
               maxlength="{config.CODE_LEN}" placeholder="{"•" * config.CODE_LEN}"
               autocomplete="one-time-code" autofocus required>
        <button type="submit">{t("enter")}</button>
      </form>
      <p class="hint below">{t("missed")}</p>"""
        if who:
            body = f"""{now}
      <p class="who">Twitch: <b>{html.escape(who)}</b> ✓</p>
      <p class="lead">{t("code_after_twitch")}</p>{form}
      {err}"""
        elif config.TWITCH_ENABLED:
            chan = (
                f'<a class="chan" href="https://www.twitch.tv/{quote(config.TWITCH_CHANNEL)}" target="_blank" '
                f'rel="noopener">{html.escape(config.TWITCH_CHANNEL)}</a>'
            )
            body = f"""{now}
      <p class="lead">{t("followers_only").replace("{chan}", chan)}</p>
      <a class="twitch" href="{config.P}/twitch?next={quote(nxt)}">{TWITCH_ICON}{t("twitch")}</a>
      <p class="hint or">{t("then_code")}</p>
      {err}"""
        else:
            body = f"""{now}
      <p class="lead">{t("code")}</p>{form}
      {err}"""

    markup = (
        ENTRY.replace("{{LANG}}", lang)
        .replace("{{TITLE}}", t("title"))
        .replace("{{PREVIEW_TITLE}}", html.escape(PREVIEW_TITLE))
        .replace("{{PREVIEW_SUMMARY}}", html.escape(PREVIEW_SUMMARY))
        .replace("{{SWITCH}}", _lang_switch(lang, nxt, t("switch_label")))
        .replace("{{BODY}}", body)
    )
    return html_page(markup, status)


def _lang_switch(lang: str, nxt: str, label: str) -> str:
    """RU | EN: the current language as text, the other as a link to /_gate/lang."""
    parts = []
    for code in texts.LANGS:
        name = LANG_NAMES[code]
        if code == lang:
            parts.append(f'<span lang="{code}" aria-current="true" title="{name}">{code.upper()}</span>')
        else:
            parts.append(
                f'<a href="{config.P}/lang?to={code}&amp;next={quote(nxt)}" lang="{code}" hreflang="{code}" '
                f'aria-label="{name}" title="{label}">{code.upper()}</a>'
            )
    return f'<nav class="lang" aria-label="Язык / Language">{"".join(parts)}</nav>'
