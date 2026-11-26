# CLAUDE.md – kinozal/

The cinema hall's server: the layout, the commands and the rules of this directory. The root `CLAUDE.md` still applies
(the en dash, English comments, comments that state the current state, docs updated in the same change). How each
mechanic works is in `docs/KINOZAL.md` (Russian): the server, OBS, the bot's `!film` and setup from scratch in one
document; a «Docs:» pointer below names the topic of a section there.

## What it is

A compose stack of its own (`name: kinozal`: Owncast, the gate, Caddy) on its own host, not a service of the bot: no
shared code, database or image. `kinozal/` never imports `src/`, `src/` never imports it, and the bot's image leaves it
out (`.dockerignore`). The bot's side of the hall is `src/twitch/local/film/` (`hall.py`, `bridge.py`).

The two meet only over the gate's HTTP API: the bot opens, renames and closes the session (`/_gate/api/session`, its
calls renew a lease) and sends Twitch lines (`/_gate/api/chat`), with Bearer `FILM_API_TOKEN` = `GATE_API_TOKEN`. Hall
lines go to Twitch from the gate itself, as each viewer, with the viewer's token and the `HALL_MARK` = `FILM_HALL_MARK`
prefix. Docs: the architecture, the chat bridge

## Layout

| Path | Holds |
|---|---|
| `docker-compose.yml` | the stack: `owncast` (RTMP in, HLS passthrough, chat; admin API on the host's 127.0.0.1:8080 only), `gate`, `caddy`; the volumes for Owncast, the gate's state and the certificates |
| `caddy/Caddyfile` | TLS for `{$KINOZAL_DOMAIN}`, security headers, `/admin*`, `/api/admin*` and `/api/integrations*` → 404, a 1 MB body and 10 s header limit, `/_gate/*` to the gate, everything else through `forward_auth` (`/_gate/auth`); applied by `caddy reload` |
| `gate/src/gate/` | the gate, FastAPI on Python 3.14, the package `gate` (modules below) |
| `gate/tests/` | the gate's tests, one file per topic; `conftest.py` – the fixtures (a fresh `gate` import per test, Twitch faked), `helpers.py` – constants, the Owncast markers, requests as the bot and a viewer. Owncast is real when `OWNCAST_URL` is set |
| `gate/pyproject.toml` | the gate's ruff rules (lint, import order, format) and pytest settings |
| `gate/requirements*.in` / `.txt` | the gate's direct dependencies, pinned / their hashed lock under Python 3.14 |
| `gate/Dockerfile` | the gate's image: `python:3.14-slim`, the lock installed with `--require-hashes`, the package from `src/gate/` (`.dockerignore` keeps bytecode out), a non-root user |
| `owncast/custom.js` | the page's and the chat's behaviour, ES5, in named sections under `// ====` banners; `TEXT`, `txt()` and `RU_READERS` at the top; the channel is the `__TWITCH_CHANNEL__` placeholder, which `style` and `setup` replace with `TWITCH_CHANNEL` on upload |
| `owncast/custom.css` | the page's styles, in named sections under `/* ==== */` banners |
| `owncast/avatar.webp`, `logo.*` | the streamer's avatar (embedded into the CSS on upload) and the logo |
| `kinozal.sh` | Owncast's settings, run on the server itself: `setup`, `style`, `link`; values from `kinozal/.env` beside it, the environment wins |
| `.env.example` | the server's `kinozal/.env`: its secrets, domain, channel, API token, the hall's Twitch app |
| `Makefile` | the checks in `python:3.14-slim`: `check`, `lint`, `format`, `audit`, `test`, `lock` |
| `tools/*.cjs` | Playwright checks of the page on a local Owncast: layout at 19 sizes, chat autoscroll, moderation |
| `.prettierrc.json` | the JavaScript's format |

The server runs the stack from a clone of the whole repository: it is installed and updated there by hand
(`git pull`, `docker compose up -d --build`, `caddy reload`). Docs: setup from scratch, updates and maintenance

The gate's package, `gate/src/gate/` (each module's docstring says what it holds):

| Module | Holds |
|---|---|
| `main.py` | the entry point: the FastAPI `app` (`uvicorn gate.main:app`), `hall_loop()` started by its lifespan |
| `routers/entry.py` | the viewer's way in and out: `/auth` for Caddy, `/enter` (the code), `/lang`, `/host` (the dock pass), `/live`, `/now`, `/logout`, `/static/` |
| `routers/login.py` | the Twitch sign-in: `/twitch`, `/twitch/callback` (the follow check, the chat user, the voice) |
| `routers/hall.py` | the hall chat for `custom.js`: `/look`, `/users`, `/bans`, `/unban` |
| `routers/api.py` | the bot's API: `/api/session` (POST, PATCH, GET, DELETE), `/api/chat`, `/health` |
| `routers/body.py` | `json_object()`: a request body as a JSON object, `{}` when empty, `None` (a 400) when not an object |
| `config.py` | the environment and the values several modules share (`P`, the cookie names) |
| `state.py` | all of the gate's state: `session`, `chatters`, `voices`, `bans`, `look`, `twins`, and their files in `/data` |
| `storage.py` | `read_json()`, `write_json()`: the state files, each replaced whole |
| `security.py` | `sign()`, `same()`, `HOST_KEY`, `client_ip()`, the brute-force limits (`throttled()`, `failed()`), `safe_next()` |
| `access.py` | who may pass: the entry cookie (`cookie_ok()`), the Twitch step before the code (`pending_*`), the streamer, the bot (`api_ok()`) |
| `session.py` | the session's life: `start()`, `retitle()`, `close()`, `expire()` (the lease), `touch()`, `info()` |
| `upstream.py` | `Upstream`, `http_sync()` and `http()` (`asyncio.to_thread`): every call to Twitch and Owncast |
| `twitch.py` | Twitch's API: the viewer and the channel, `may_watch()`, the app token, `revoke()`, `revoke_later()` |
| `owncast.py` | Owncast's APIs: `admin()`, integration tokens, `integration()`, `chat_identity()`, `chat_users()`, `set_name()` |
| `twins.py` | the twins «nick [TW]»: their tokens, `in_history()`, `say()` with `TwinDisabled` / `TwinUnverified` |
| `voices.py` | the viewers' Twitch tokens: `remember()`, `forget()`, `forget_voices()`, `say()` (hall → Twitch) |
| `look.py` | the chat look: `remember()`, `channel_emotes()` (emotes and badges from Helix), `viewer_color()` |
| `moderation.py` | bans by Twitch account: `banned()`, `sync_bans()`, `unban()`, `bans_lock` |
| `bridge.py` | `hall_loop()`: `relay_once()` (hall → Twitch), `keep_name()`, the ban sync, the lease |
| `pages.py` | the gate's pages: `page()`, `chat_handoff()`, `html_page()` with its CSP, `STATIC`, `lang_of()`, `with_lang()` |
| `texts.py` | the entry page's texts (`TEXT`, `tr()`) in both languages and `browser_lang()` |
| `chat_markup.py` | chat line text: `plain()` (Owncast markup out), `escape_markdown()`, `hall_markdown()` |
| `web/` | the entry page: `entry.html` template, `entry.css`, `entry.js`, `handoff.js`, `logout.js`, `twitch.svg`, read at start |

## Commands

```bash
make -C kinozal                    # The target list; a bare make runs nothing
make -C kinozal check              # Before a merge: ruff (lint, format), shellcheck of kinozal.sh, the gate's tests
make -C kinozal format             # The gate's format and safe fixes, by the same ruff as lint
make -C kinozal audit              # Known vulnerabilities in the gate's dependencies
make -C kinozal test               # The gate's tests; with OWNCAST_URL and OWNCAST_ADMIN_PASSWORD, the chat ones too
make -C kinozal lock               # After editing gate/requirements*.in
./kinozal.sh setup                 # On the server: every Owncast setting; refused while a stream is live
./kinozal.sh style                 # On the server: the page's CSS, JS and offline text; safe during a stream
./kinozal.sh link                  # On the server: the OBS chat dock link
```

A local Owncast takes `OWNCAST_API`, `OWNCAST_ADMIN_PASSWORD` and `TWITCH_CHANNEL` from the environment:
`OWNCAST_API=http://127.0.0.1:18080 OWNCAST_ADMIN_PASSWORD=testpass TWITCH_CHANNEL=somechannel kinozal/kinozal.sh style`.

The root `make check`, `make lint` and `make audit` leave `kinozal/` out. A local Owncast with a stream, the whole
stack behind a local Caddy and the Playwright setup: docs/KINOZAL.md, the local checks.

## Config and secrets

- **The bot's `.env`** holds only its side of the hall: the `FILM_*` section, with `FILM_URL` (`https://` + the
  server's `KINOZAL_DOMAIN`) and `FILM_API_TOKEN` (= `GATE_API_TOKEN`).
- **The server's `kinozal/.env`** is filled in by hand from `.env.example`: the server's own secrets
  (`OWNCAST_ADMIN_PASSWORD`, `OWNCAST_STREAM_KEY`, `GATE_SECRET`), generated there once, and the values shared with
  the bot. Compose reads it for `${...}`; the required ones are `${VAR:?}`, so the stack does not start without them.
  `kinozal.sh` reads the same file; a value in it holds no trailing comment, since the script takes the whole line.
- **`.env.example` here is English**, like the rest of `kinozal/`; the root `.env.example` stays Russian.
- **No tracked file holds an operator's host, domain, channel or secret.** A new server value goes to `.env.example`
  and docs/KINOZAL.md, «Настройки и секреты». Docs: settings and secrets

## Invariants that are easy to break

**The gate**
- **The API is a contract between two deploys.** A change to `/_gate/api/*` or to `HALL_MARK` keeps the running bot
  working, or ships with the bot's change in `src/twitch/local/film/`.
- **Every handler is `async`**: the gate's state (`session`, `chatters`, `voices`, `bans`) changes on the event loop
  only. Blocking calls go through `http()` (`asyncio.to_thread`).
- **Viewers' Twitch tokens live in memory, for the session only.** A gate restart loses them, so it restarts only with
  no open session (`/_gate/now` → `"open":false`). A token kept after `forget_voices()` would never be revoked: code
  after an `await` checks that the session is still the same one.
- **`hall_loop()` must never end**: it alone relays the hall to Twitch, syncs the bans and closes a lapsed lease. Any
  failure inside is logged and the loop goes on.
- **The hall nick reset (`owncast.set_name()`) keeps its websocket open 0.5 s after `NAME_CHANGE`**: closed at once,
  Owncast 0.3.0 stops taking chat lines from other viewers. `test_hall_lines_go_to_twitch_as_the_viewer` right after
  the rename test (`tests/test_hall_to_twitch.py`, which keeps them in that order) catches it, on a local Owncast
  with a stream.
- **Secrets are compared with `same()`**, never `hmac.compare_digest` on `str`: it raises on non-ASCII input.
- **A return address goes through `safe_next()`**: a path on this site only, no control characters or spaces
  (browsers drop a tab, and `/<tab>/evil.com` becomes `//evil.com`).
- **A twin's ban is kept by Twitch account and outlives the chat history**, the only place Owncast shows it. A 401 on a
  twin is checked for a ban before a new token is taken, and an unexplained 401 never falls back to the shared «Twitch»
  sender. Docs: moderation and bans
- **The entry page's card is `.box`**: its RU | EN script fetches the switch link and swaps that element in place.
  A page the gate renders keeps its whole content inside it.
- **Russian is only what the web interface shows**: the page texts in `TEXT` (`custom.js`, `gate/src/gate/texts.py`) in
  both languages, Owncast's translation and the settings `kinozal.sh` posts (name, summary, offline text). Logs
  (`print`), API errors, the tools' output and test data are English. A test keeps Cyrillic only where non-ASCII input
  is the point, or where it checks a Russian interface text.
- **Gate pages run no inline script**: `html_page()` sends a CSP with `script-src 'self'`. A script or style goes into
  `gate/src/gate/web/` (served from `STATIC` by name only), and a value it needs goes into a data attribute,
  HTML-escaped.
- **Modules import each other as modules** (`from gate import config, state`) and read `state.session`,
  `config.TWITCH_ENABLED`, `upstream.http()` at call time. A test patches a name in the one module that owns it; a name
  imported on its own (`from gate.state import session`) is a copy that neither a reassignment nor a patch reaches.
  Mutable state lives in `state.py`, caches with the module that fills them (`twitch.channel_id`, `owncast.chat_token`).
- The image copies the whole package: a new module needs no `Dockerfile` change, a new route module joins the
  `include_router` list in `main.py`.

**The page**
- `custom.js` is ES5: Owncast's page runs it as is. `gate/src/gate/web/*.js` keep the same style. Format by
  `.prettierrc.json` (width 120, no trailing commas).
- A new text of the page or the entry page goes into `TEXT` (`custom.js`, `gate/src/gate/texts.py`) in both languages,
  never inline.
- Owncast takes its language only from `?lang=`, else from the browser; the gate adds `?lang=en` on the way to the
  site, `custom.js` keeps it. Docs: the languages
- **No `@import` in `custom.css`**: Owncast inserts it into a `<style>` after the page loads, and Firefox holds a
  sheet with an import until the imported URL answers, so the page shows Owncast's own look first. The fonts are
  linked by `custom.js` «fonts»; the offline banner's text stays hidden until they are in (`kz-fonts`).
- **A chat feed row is never hidden with `display: none`**: react-virtuoso cannot measure a 0px row and stops drawing
  the feed. A row to hide gets 1px.
- Owncast selectors match by substring (`[class*="…"]`), never `[class$=…]`: antd appends classes.
- React redraws the page: own elements are inserted again from an observer; own buttons over the player need
  `z-index` > 100; own key handlers run in the capture phase. The rest: docs/KINOZAL.md, the known traps.

**The server**
- A change on the live hall server is outward-facing: only on the streamer's explicit yes.
- Caddy keeps Owncast's own CSP (`script-src` nonce): a header that sets `Content-Security-Policy` in the Caddyfile
  would replace it. A path Owncast's page never calls stays closed in the `@internal` matcher.
- The Caddyfile's site-wide `header` block covers the gate's answers too. A header to drop from Owncast's answers only
  goes into `header_down` of its `reverse_proxy`: `/_gate/now` keeps its `Access-Control-Allow-Origin` for the OBS
  placeholder, which reads it from another origin.
- Caddy reads the mounted `caddy/` directory, so a broken Caddyfile loops it on its next restart: a new one is checked
  with `caddy validate` before `up` and `caddy reload`. Compose does not see a mounted file change.
- The gate is recreated only with no open session (it loses every viewer's Twitch token), Owncast only with no session
  and no stream (it drops the stream and every player). A closed session does not mean no stream: Branch Output sends
  while its eye is on.
- `setup` rewrites the video settings and drops a live stream: it goes on only on a plain `"online":false` from
  `/api/status`; a stream, silence or an error refuses it (`KINOZAL_FORCE_STREAM=1` overrides).
- `kinozal.sh` holds no operator-specific value and runs where the stack runs: no SSH, no copy of a secret elsewhere.
  A value bound for a URL or a `sed` pattern is checked for its shape (`checked()`).

## Style

- Comments, logs, tool output and test data are English; Russian stays only in what the web interface shows.
- `custom.js`, `custom.css`, `tools/*.cjs`, `kinozal.sh` and the `Makefile` are split into named sections under
  `====` banners as wide as the existing ones, a blank line above a banner and none below; functions and multi-line
  blocks are separated by a blank line. `docs/KINOZAL.md` and the tools refer to `custom.js` sections by name.
- `kinozal.sh`: 2-space indent, `case` branches indented, clean under shellcheck.
- The gate's Python: `ruff format` (width 120, hanging indents rather than alignment with the bracket), checked by
  `make -C kinozal lint`, applied by `make -C kinozal format`. A module keeps to one part of the mechanics; one that
  grows past a few hundred lines splits.

## When changing things

- A change to the gate: `make -C kinozal check audit` (with a local Owncast for the chat tests); a change to its API
  also updates `src/twitch/local/film/` and its tests in the same change, or stays compatible with them.
- A change to `custom.js` or `custom.css`: `node kinozal/tools/*.cjs` on a local Owncast; `moderation.cjs` and
  `chat-scroll.cjs` also with `firefox`, the streamer's browser.
- A change to `gate/src/gate/web/`: Prettier, then the entry page, the code login and «Выйти» in a browser behind a
  local Caddy, with no CSP violation in the console. A blocked script fails silently.
- The gate's dependencies: edit `gate/requirements*.in`, then `make -C kinozal lock`; `lock` keeps the pins it has,
  so a transitive upgrade needs a run with `--upgrade`.
- `docs/KINOZAL.md` is the one reference: a change to behaviour, config or layout updates it in the same change, and
  this file when a rule or the layout changes.
