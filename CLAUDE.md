# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository: the rules, the layout and the invariants. How each mechanic works is in `docs/BOT.md`.

## Project Overview

Twitch chat bot powered by Gemini 3.8 Flash. Commands (`!who`, `!ask`, …) work as plain chat text. Free text is answered only when it contains a `сосур*` / `secur*` word (сосурян, сосурити, securityexpert, etc.), an `@botname` mention, or is a reply to a bot message. Stores full chat history in SQLite with FTS5 search and long-term memory.

## Documentation

| File | Holds | Language |
|---|---|---|
| `README.md` | what the bot can do, installation, operations, CLI, the command and environment variable tables, project tree | Russian |
| `docs/BOT.md` | the one detailed reference: every mechanic, its numbers and the failure modes it guards against | Russian |
| `CLAUDE.md` | this file: rules, layout, module map, invariants | English |
| `docs/CONTENT.md` | everything the bot says, read by the code; `<!-- … -->` notes explain keys | Russian |
| `docs/TTS.md` | the TTS server the Discord voice uses: model, install, profiles, launch, API, performance, diagnostics – it runs on another machine and is not part of this code | Russian |
| `docs/index.html` | the bot map: sections, limits and pauses per viewer status, a standalone page published to GitHub Pages by `make pages`; its numbers are written by hand | Russian |
| `.env.example` | every environment variable with a comment | Russian |

`BOT.md` and `CONTENT.md` below mean the files in `docs/`.

Before changing a feature, read its section in `BOT.md`.

## Commands

```bash
# Run in production: Docker Compose. The image carries the dependencies; the working
# tree, mounted read-only at /app, is the code that runs (a restart runs the checked-out
# branch); the database and tokens live on a volume. Two services: bot and roll (the game)
docker compose up -d
docker compose logs -f              # make logs; make logs S=roll – one service
docker compose restart bot roll     # make restart; one service: docker compose restart roll

# The game service without a container (debugging): beside a bot run from the host, with
# ROLL_URL=http://127.0.0.1:8789 for the bot and BOT_API_URL=http://127.0.0.1:8790 for the game
cd data && BOT_DB_PATH=chat_history.db BOT_API_URL=http://127.0.0.1:8790 ../venv/bin/python3 ../roll.py

# A maintenance command runs in the same image. Paths are the container's: /app is the
# tree (read-only), /data holds the database and the tokens
docker compose run --rm bot /app/bot.py --backup /data/copy.db

# New Twitch tokens (bot or channel account): the container publishes no OAuth port
make oauth

# Run without a container (debugging). Always use venv python, not system python;
# from data/ it finds the container's .tio.tokens.json; the DB path is explicit, the default is the repo root
cd data && BOT_DB_PATH=chat_history.db ../venv/bin/python3 ../bot.py

# Upload lore from txt files (bot doesn't start)
./venv/bin/python3 bot.py --upload-lore lore.txt [file2.txt ...]

# Preview without writing to DB
./venv/bin/python3 bot.py --upload-lore lore.txt --dry-run

# Clear knowledge and re-import
./venv/bin/python3 bot.py --upload-lore lore.txt --source lore.txt --clear-lore

# Clear knowledge only (no import) – one source, or everything with --all; with --dry-run only counts
./venv/bin/python3 bot.py --clear-lore --all
./venv/bin/python3 bot.py --clear-lore --source article.md

# Other lore formats and the sources in the table
./venv/bin/python3 bot.py --upload-lore result.json --format telegram   # Telegram Desktop export
./venv/bin/python3 bot.py --upload-lore article.md --format text        # cut into 1–3 sentence pieces
./venv/bin/python3 bot.py --lore-sources

# List all saved facts (the retired !fact table)
./venv/bin/python3 bot.py --list-facts

# The bot's memory over the whole history (once; after that the bot keeps it itself)
./venv/bin/python3 bot.py --build-memory
./venv/bin/python3 bot.py --build-memory --dry-run --limit 3   # print 3 chronicles and 3 profiles, write nothing
./venv/bin/python3 bot.py --clear-memory
./venv/bin/python3 bot.py --clear-memory --dry-run         # only counts the rows

# Context probe: real addressings of the bot, `now` (the plain chat-window context) vs `new` (what the bot sends), side by side; costs ~$0.01 per question
./venv/bin/python3 bot.py --probe-context --limit 8
./venv/bin/python3 bot.py --probe-context 33839 35280 --samples 2
# Generation settings instead of contexts: `bot` vs `candidate` (temperature, thinking budget, persona from a file, model)
./venv/bin/python3 bot.py --probe-context 33839 --temperature 1.4 --thinking 0 --system persona.txt

# Consistent DB copy via sqlite backup API (safe while the bot runs)
./venv/bin/python3 bot.py --backup [path.db]

# Compact the DB file
./venv/bin/python3 bot.py --vacuum

# Pull channel emotes from Twitch into CONTENT.md (merge, nothing is removed)
./venv/bin/python3 bot.py --sync-emotes
./venv/bin/python3 bot.py --sync-emotes channel global --dry-run
./venv/bin/python3 bot.py --sync-emotes channel --replace-emotes   # rebuild the list from scratch

# Lore import runs init_db() automatically – no bot restart needed.
# Logic in src/cli/knowledge.py. See BOT.md for detailed lore file format guide.

# Setup venv from scratch: Python 3.11, as in the image (system python3 may be newer)
make venv            # = python3.11 -m venv venv && pip install --require-hashes -r requirements.txt -r requirements-dev.txt

# Before a merge (there is no CI)
make check           # ruff + pytest
make audit           # known vulnerabilities in requirements.txt and requirements-dev.txt; the ignores (PyNaCl, pinned by discord.py; transformers, pinned below 5 by ruaccent) are explained in the Makefile
make lock            # recompile requirements*.txt after editing requirements*.in
```

## Architecture

The bot runs as **services**, one process and one compose container each, all from one image: `bot.py` – the bot (a CLI command, or `run_bot()`, which starts it on every platform it is configured for) and `roll.py` – the game service (`src/roll/main.py`). Both entry points are thin. All logic lives in `src/`, split **first by service or platform, then by kind**. The voice and the cinema hall are the next to move out the same way.

### Where code goes

| Package | Holds | Put here |
|---|---|---|
| `src/core/` | the shared skeleton: config, texts, DB, logging, utils, cooldowns, background tasks, the status ladder, the OBS overlays' SSE server | nothing platform- or feature-specific |
| `src/gemini/` | the brain: everything that costs a Gemini call and is not tied to a platform – prompts, context, the fallback ladder, the cleanup of an answer, long-term memory, the `!ascii` download and rendering | anything that builds prompts or talks to Gemini |
| `src/cli/` | `bot.py --…` commands; the bot does not start | a maintenance command |
| `src/twitch/` | everything tied to Twitch: `bot.py` (the twitchio `Bot`) and the Twitch side of every feature | see the three rows below |
| `src/twitch/core/` | the Twitch skeleton: command registry, chat dispatcher and its gate, per-stream limits, badges → tier, replies in chat, chat sockets, tokens, the stream tracker, follower checks | nothing feature-specific. Twitch features depend on it, never the reverse – the one exception is `component.py`, which registers every feature's commands |
| `src/twitch/gemini/` | Twitch handlers of generated answers: free text, `!ask`, `!who`, `!versus`, `!summary`, `!ascii`, proactive remarks, the response pipeline | a Twitch command that generates text (`kind=Kind.GEMINI`) |
| `src/twitch/local/` | сосурян's Twitch features without Gemini: simple commands, follow replies, emote spam, the periodic command reminder and the `mascot/` OBS overlay feed | a small command or event reply served from SQLite + `CONTENT.md` |
| `src/twitch/roll/` | the bot's side of the game service: the link that carries commands, chat and stream events and redemptions to it, and the Twitch half of the channel-points rewards | anything the bot does on Twitch for the game |
| `src/discord/` | everything tied to Discord: `bot.py` (the discord.py client, kept up by `DiscordService`: the voice channel by `!join` / `!leave`, the voice by `!voice`, `!help`) and `local/voice/` – the bot's answers spoken through the TTS server | a Discord feature, inside the package of its kind (`discord/local/…`, later `discord/gemini/…`) |
| `src/roll/` | the game service, «залупа стрима»: throws, what a reward does, perks, curse-lift announcements, the month's board, its OBS overlay, its API for the bot. It never talks to Twitch: everything goes through the bot | anything about the game itself |

Shared code (`src/core/`, `src/gemini/`, `src/cli/`) never imports a platform or service package, and the platforms and services never import each other: Twitch hands an answer that reached chat to `src/core/speech.py`, the Discord voice listens there; the bot and the game talk only over their internal HTTP APIs (`src/core/service.py`: the bot's `src/twitch/core/api.py`, the game's `src/roll/service.py`). The one exception: `gemini/memory/build.py` reads a session's game results from `src/roll/storage.py` – plain SQL on the shared database file. A new platform gets its own `src/<platform>/` with the same kinds inside, and takes the brain from `src/gemini/` rather than from another platform.

**The game service.** The bot is the only Twitch gateway: it holds both tokens, passes the game's commands through its own gate, then sends them, every chat message (perks, statuses), the stream changes and each redemption to the game; the game replies and speaks in chat through the bot's API. Every call from the bot carries the stream state (session, live, rewards), so the game never acts on an older session. Either side survives the other being down: without the game the bot answers `texts.roll_unavailable` and refunds a redemption with `texts.reward_unavailable`; without the bot the game logs its lost lines and keeps the overlay. Both processes write the one database file; SQLite serializes them (`BEGIN IMMEDIATE`, 30 s busy timeout). BOT.md «Служба игры»

A feature that grows its own tables, texts and background loops gets **its own subpackage inside the package of its kind** – `twitch/local/<feature>/` like `twitch/local/mascot/`, or `twitch/gemini/<feature>/` if it generates text – instead of spreading across several files of `local/` and `core/`. One that grows into a service of its own moves to `src/<service>/` with its own entry point and compose service, like the game: what it needs from the bot is a protocol in its package (`src/roll/port.py`), served over the bot's API. Table schema and migrations still go into `init_db()` in `src/core/db/schema.py` (one place to keep startup idempotent); the feature's queries live in its package, like `src/roll/storage.py`. Each package's `__init__.py` lists its modules. Paths to `CONTENT.md` and `chat_history.db` live in `src/core/paths.py`: they default to `docs/CONTENT.md` and `chat_history.db` in the repository root, found via `Path(__file__).parents[2]`, and `BOT_DB_PATH` / `BOT_CONTENT_PATH` move them – that is how the container keeps its data on a volume instead of inside the image. `db/connection.py` and `content.py` import `DB_PATH` / `CONTENT_PATH` under their own names, because callers import `get_db()` rather than the path, and the tests patch them there.

### Modules

One line per module: what it holds. How a feature behaves and why lives in `BOT.md`; the references below name its section.

- `bot.py` – the entry point: `__main__` delegates to `src/cli/main.py`, otherwise `run_bot()` – logging, config and content checks, signal handling, the Twitch bot and, beside it, the Discord bot (`start_discord()` / `stop_discord()`: its own task, a failure there never stops Twitch), `close_db()` on the way out. BOT.md «Жизненный цикл бота»

**core** (shared)
- `config.py` – every environment variable, parsed and validated (`_env_int`, `_env_float`, `_env_bool`, `_env_percent`, `_env_ids`, `_interval_range()`), in classes `Files`, `Clock`, `Logging`, `Twitch`, `Gemini`, `Chat`, `Caps`, `Cooldown`, `Quota`, `Follow`, `PerStream`, `Picture`, `Clip`, `Stream`, `Film` (with `_env_roles()`), `Roll`, `Rewards`, `Context`, `Memory`, `Pulse`, `Help`, `Proactive`, `Emote`, `Mascot`, `Discord` (with `missing()`), `Voice`
- `paths.py` – `DB_PATH`, `CONTENT_PATH`, `ACCENT_PATH`: `chat_history.db` in the repository root, `docs/CONTENT.md` and `ruaccent/`, or `BOT_DB_PATH` / `BOT_CONTENT_PATH` / `BOT_ACCENT_PATH` (the image sets it to the models it carries)
- `content.py` – `CONTENT.md` access (`Content.prompt/label/text/items`), mtime cache, `REQUIRED`, `validate_content()`
- `database.py` – facade re-exporting `src/core/db/`: callers import every query from here
- `db/connection.py` – the shared connection (`get_db()`, `close_db()`, `reopen()`), `transaction()`, `backup_db()`, `vacuum_db()`
- `db/schema.py` – `init_db()`: named `STEPS` for every table, `SCHEMA_VERSION`
- `db/chat.py` – `chat_messages`: saving, chat windows, previous/last session (cached), `!stat` numbers, a nick's last message time (`!lastseen`)
- `db/interactions.py` – `bot_interactions`: tagged answers, the viewer's dialogues
- `db/knowledge.py` – `knowledge` and `facts`: FTS5 search, the random «language» sample
- `db/quota.py` – `bot_uses`: hourly quota, channel ceiling, per-stream counts, the all-time count that numbers clips
- `db/streams.py` – `streams`: stream id → session, start, end
- `db/state.py` – `bot_state`: `get_state()` / `set_state()`, small settings kept across restarts (the Discord voice channel choice)
- `activity.py` – `ChatWatch`: «has anyone written since» for the chat loops
- `cooldowns.py` – `Cooldowns` on the monotonic clock
- `speech.py` – `say(text, source)` / `listen()`: an answer that reached a platform's chat, handed to whoever voices it; never blocks or raises. BOT.md «Голос в Discord»
- `tier.py` – `Tier` and `by_tier()`: the one status ladder behind every per-status number; `tier_sub_hint()`, the «a subscription widens it» tail of a limit refusal, only where the subscriber's limit is higher
- `sse.py` – `Feed` (one SSE event name: listeners, `publish()`, the state a new page starts from, pings, a stuck listener drops old items) and `serve()`: one aiohttp server on `MASCOT_HOST`:`MASCOT_PORT` for every overlay, open streams closed first on shutdown. BOT.md «Канал для OBS»
- `service.py` – `ServiceClient` (one service's API: a kept session, `SERVICE_TOKEN`, a deadline per call; raises `ServiceUnavailable`, with `timeout` when the work may have been done), `require_token()`, `serve()` (an API until cancelled; a busy port is logged)
- `tasks.py` – `BackgroundTasks`: loops by name, never started twice; `once=True` for a task meant to finish; `cancel(name)` for one task
- `logging_setup.py` – `setup_logging()`, `make_formatter()` (log times in `BOT_TIMEZONE`, secrets replaced by `redact()`)
- `utils.py` – shared helpers: `SOSUR_RE`, `clean_nick`, `parse_nick` (one word that looks like a login: `!lastseen`, `!rollstat`, the rewards that take a nick), `safe_format`, `defuse`, `find_banned` (the stop list), `local_time`, `random_delay`, `gather_cancelling`. `SOSUR_RE` lives here rather than in `component.py`: `db/schema.py` backfills `chat_messages.addressed` with it, and importing the dispatcher there would be circular

**gemini** (shared)
- `client.py` – `get_client()`, `generate()` (chat, with the answer deadline), `generate_checked()` (the reason there is no text), `make_gen_config()`, `background()` (the slots memory, the pulse and the fact check share, `use_all_slots()` for the CLI backfill), `using_model()` (another model for one task's requests, the probe's candidate), `SAFETY_OFF` / `SAFETY_CHECK`, `usage`, `PRICES` / `cost_estimate()` (the one place the Gemini prices live, per model). BOT.md «Шаг 10: Вызов Gemini»
- `context.py` – `ContextBuilder`, `chat_chars()`, `tail_within()`
- `ladder.py` – `Rung`, `walk()` (the fallback ladder), `unique_rungs()`
- `answer_context.py` – free-text context: `Question` (with the pulse), `ladder()` (pulse, the last `CONTEXT_CHAT_MESSAGES`, dossiers, chronicle – not the raw stream), `answer()`; shared with `--probe-context`. BOT.md «Контекст Gemini»
- `pulse.py` – the pulse: what the chat is about right now, retold in a few lines (`build()`, `refresh()`, `current()` – the cache, stale after two intervals or with a new session). BOT.md «Пульс чата»
- `dossier.py` – `people()`: a chatter's dossier as one context line (headline, `storage.pick_facts()` without resting facts, catchphrases, relations); the cap counts chatters with a dossier, not words
- `fact_rest.py` – a worn-out dossier fact rests: `resting()`, `answered()` (count the facts shown in an answer that reached chat, check in the background which it used – `used()`, `prompts.fact_check`). BOT.md «Отдых фактов досье»
- `output.py` – cleanup of an answer before chat: `cleanup_response()`, `trim_to_sentence()`, `split_into_chunks()`, `strip_*`, `fix_dashes()`, `TWITCH_MSG_MAX`. BOT.md «Шаг 11»
- `summary.py`, `who.py` – the context of `!summary` and of `!who` / `!versus` (a random sample of the dossier)
- `memory/storage.py`, `memory/build.py` – long-term memory: chronicles, events, dossiers (`chatter_profiles` – headline, relations, catchphrases; `chatter_facts` – retold facts merged by `apply_dossier()`), `memory_loop()`. BOT.md «Память бота»
- `picture/fetch.py`, `picture/render.py` – `!ascii`: safe download, braille rendering. BOT.md «Шаг 7ж»

**twitch**
- `bot.py` – `Bot(commands.Bot)`, the Twitch assembly point: lifecycle (`setup_hook`, `event_ready`, `event_oauth_authorized`, `close()`), EventSub subscriptions, `send_chat_message()`, `fetch_schedule()`, the stream state (`stream`, `session_id`, `stream_went_online()` / `_offline()`, `_sync_stream()`), background task start, the game (`_game`: `RollAdapter`, or None without `ROLL_URL` – the bot only calls its hooks), `_start_overlays()`: the mascot's page and the game's services, signal handlers taken back from aiohttp (`install_signal_handlers()`). BOT.md «Жизненный цикл бота»

**twitch/core**
- `component.py` – `ChatComponent`: the registry of all commands (a service's own ones through `set_game_commands()`), `event_message`, the pure `route()` and the gate `_gate()` (follow, cooldown, quota), `event_follow`. BOT.md «Обработка сообщений»
- `commands.py` – `CommandContext`, `CommandEntry` (`public` – served without the follow check; `roles` / `refusal` – who may use it and what anyone else is told; `channel` – a command about the channel, listed by `!channel`), `CommandRegistry` (`set_fallback()` – commands looked up after the registered ones, `channel_triggers()`), `Kind` (`StrEnum`: its values are the plain strings stored in cooldown keys and `bot_uses.kind`)
- `stream.py` – `StreamTracker` (session = stream, resume rules), `watch_stream()`, `fetch_live_stream()`, `end_from_vod()`. BOT.md «Сессии»
- `chat_socket.py` – `SocketWatch` (resubscribes when no socket carries the chat or the redemption subscription; after a failure only the minute check retries), `check_private_api()`, `keep_migrated_sockets()` (keeps a migrated socket in twitchio's registry)
- `tokens.py` – the bot's and the channel's tokens (`add_bot_token()`, `add_broadcaster_token()`), token saving, OAuth links (`OAUTH_SCOPES_FULL`: what the bot reads as the channel's moderator – followers and the chat list; `add_login_routes()` – the short `/bot` and `/channel` on twitchio's OAuth server, which a terminal cannot wrap and a copy cut short; `print_login_links()` – both, printed by `make oauth`), `token_problem()`, `token_dead()` (a revoked authorization: retrying is pointless)
- `port.py` – `BotPort` / `StreamBot`: what features need from the bot (incl. `create_clip()`, `fetch_chatters()` – Twitch's chat list for the mass flasks, `fetch_schedule()` – the channel's schedule). Core and the features never import `src/twitch/bot.py`; `tests/test_bot.py` checks that both `Bot` and `tests/fakes.py`'s `FakeBot` have every member, and what the follow gate needs (`channel_id`, `create_partialuser()`)
- `viewer.py` – `tier_of()`: chat badges → `Tier` (a subscription outranks the moderator and VIP badges); `sub_hint()` – `tier_sub_hint()` by the badges
- `limits.py` – `limit_for()` (one ladder), `deny()` (a limit refusal at most once per 30 s) and `PerStreamLimit`: per-stream limit of a command (`!ask`, `!who`, `!versus`, `!summary`, `!ascii`, `!clip`), one call per viewer at a time, count only what reached chat
- `followers.py` – `FollowerCache`: follower check via Helix with a TTL, fails open; one cache for chat and rewards, `forget()` on a new follow
- `api.py` – `BotApi`: the bot's internal API for the services (`/state`, `/chat`, `/reply` to a message a service is answering, `/follows`, `/chatters`), on `BOT_API_HOST`:`BOT_API_PORT`, the compose network only
- `replies.py` – `reply()` (a reply in chat that never raises), `reply_to_bot()` (the bot's line a message replies to)

**twitch/gemini**
- `responder.py` – `respond_and_save()`, `send_chunked()`, moderation, CAPS, emote
- `commands.py` – handlers of free text, `!ask`, `!summary`, `!who`, `!versus`; `LIMITS`
- `picture.py` – the `!ascii` handler with its cache. BOT.md «Шаг 7ж»
- `proactive.py` – `proactive_loop()`
- `pulse.py` – `pulse_loop()`: rebuilds the pulse every `PULSE_INTERVAL_MINUTES` during a stream while people write; a build that gave nothing is retried on the next round

**twitch/local**
- `commands.py` – `handle_help_index` (`!help`), `handle_help` (`!bot`), `handle_stats`
- `channel.py` – the channel's own commands (`!tg` …) from `## channel` of `CONTENT.md`, each reply wrapped in `texts.channel_reply` unless it names `{user}` (`_render()`): `ChannelCommands` (`resolve()`, the registry's fallback; `check()` logs clashes with bot commands and replies too long for Twitch on every edit; `listing()` and `handle_help()` – `!channel`, the file's commands, then the registry's `channel` entries). BOT.md «Шаг 7а4»
- `clip.py` – `!clip`: `parse()`, `default_title()`, `handle_clip`, `make_clip()` (create the clip as the bot, wait until Twitch lists it). BOT.md «Шаг 7з»
- `film.py` – `!film`: the hall's link, session code and film title, kept by its gate (`request()` – any gate call, `gate()` – the session API, `GateError`, `NoSession`, `is_open()` – the last answer, read by the bridge); the handlers of `!film`, `!film start [title]` (refused while a session is open, under `_start_lock`), `!film title`, `!film stop`; `close_after_stream()` – the `film_close` task started by `Bot.stream_went_offline()` after a stream of ours, cancelled on stream start; `heartbeat_loop()` – the `film_heartbeat` task, started even without the channel id: renews the gate's lease and feeds `Reminder` (the link and the code every `FILM_REMIND_MINUTES`); `close_on_shutdown()` – from `Bot.close()`. Roles – `FILM_INFO_ACCESS` / `FILM_CONTROL_ACCESS`, checked by `ChatComponent._has_role()`. BOT.md «Шаг 7и»
- `film_bridge.py` – the chat bridge Twitch → hall while a session is open (`FILM_BRIDGE`, the `film_bridge` task `bridge_loop()`): `from_twitch()` – a viewer's or the bot's line, with `look(message)` (colour, emotes, badges), queued for the gate's `POST /_gate/api/chat`; `from_hall()` – a hall line without its `FILM_HALL_MARK`, which the dispatcher routes like any line. Hall → Twitch is the gate's: it posts as the viewer, with the viewer's own token. BOT.md «Мост чатов кинозала»
- `schedule.py` – `!schedule`: `fetch()` (a raw Helix request, 404 – no schedule), `parse()` (the nearest upcoming slots not cancelled, `SLOTS` of them; a vacation while it lasts), `render()`, `handle_schedule` (Helix's answer cached for `CACHE_SECONDS`, a failure is not). BOT.md «Шаг 7а6»
- `project.py` – `!project` (anyone, a channel command) and `!project title <text>` (the streamer's): the text in `bot_state.channel_project`, kept until the next title. BOT.md «Шаг 7а7»
- `lastseen.py` – `!lastseen <nick>`: the nick's last chat line (`get_last_message_time()`), «сегодня / вчера / date» in `BOT_TIMEZONE` (`when()`); listed by `!channel` but with the usual gate and a personal cooldown, since the nick differs per call. BOT.md «Шаг 7а8»
- `follow.py` – `handle_follow()`: a thank-you once per viewer, a few a minute
- `emote_spam.py` – `emote_spam_loop()`
- `help_announce.py` – `help_loop()`, `note_help_shown()`

**twitch/local/mascot** – the OBS overlay's mood from chat activity (BOT.md «Маскот: оверлей по настроению чата»)
- `mood.py` – `target()` (chatters → mood and dance rate), `MoodTracker` (window, up at once, down a level per step), the module-level `tracker`
- `feed.py` – `on_chat()` (called first in `event_message`), `feed` and `add_routes()` (`/mascot/events`, `/mascot/state`), `mascot_loop()` (the recount)

**roll** – the game service, «залупа стрима» (BOT.md «Служба игры», «Шаг 7а», «Награды за баллы канала»)
- `main.py` – the process (`roll.py`): config and content checks, `init_db()`, the stream state from the bot (`RemoteBot.sync()`), the API and the overlay servers, `on_stream()` for a restart mid-stream, the `state`, `curse_lift` and `choke` loops, shutdown on a signal
- `service.py` – `COMMANDS` (trigger, handler, public – declared to the bot by `/commands`), `GameService`: the API the bot calls (`/commands`, `/command`, `/events/chat`, `/events/stream`, `/rewards/specs`, `/redemption`, `/rewards/applied`), each call updating the stream state first; `HANDLERS`, `on_stream()` (titles, regalia, old curses, perks – idempotent)
- `remote.py` – `RemoteBot`: `RollPort` over the bot's API (`/chat`, `/reply`, `/follows` – fails open, `/chatters`, `/state`), the stream state as the bot last sent it and as `state_loop()` asks for every minute, the game's own `Cooldowns`
- `port.py` – `RollPort`: what the game needs from the bot – session, stream and rewards state, a chat line, the chat list, the game's cooldowns
- `request.py` – `Request`: a game command without twitchio (user, tier, args; `reply()` as it goes, `deny()` once per 30 s) and `Gate` – what the bot's gate must give back (`refuse`, `clear_cooldown`, `clear_public`)
- `specs.py` – `RewardSpec`, `specs()`: the rewards as the game defines them (titles and descriptions from `CONTENT.md`, costs and limits from `.env`), sent to the bot as dicts
- `rules.py` – the rules as pure functions: of `now` for throws, curses and pauses, of the status for `free_limit()`
- `game.py` – the only code that mutates `rolls`, under one lock: `free_throw()`, `redeem()` (incl. the random flask, its weights `FLASKS`, and the mass flasks over everyone in chat), `refuse()` (a refusal before the game, journaled against redelivery), `lift_expired_curses()`, `drop_finished_curses()`, `status()`, perks
- `storage.py` – queries on `rolls`, `roll_actions` (incl. `get_applied()` for the bot's stale redemptions), `roll_throws`, `roll_perks`, `roll_tiers`, `roll_chokes`
- `command.py` – `handle_roll`, `handle_rollstat` (on a `Request`) (yours, or another player's with a nick: their limit by `roll_tiers`, the `_other` texts)
- `board.py` – `!rollboard`, «Китежанин месяца» (`handle_rollboard`, a `public` entry: one shared pause, which a refused «!rollboard 5» gives back): the month's three lines by the titles of its finished streams (`standings()`, one title of each kind per stream by `get_session_titles()` without the streamer, the live stream and a stream with a single viewer left out; a tie on the first place shares it, `ROWS` lines at most; below it whoever reached the count first, `TOP` lines in all), `final()` – a finished month's board: a tied first place goes to one by lot, drawn once and kept in `bot_state.roll_board_winners:YYYY-MM`, `announce()` – called by `handle_roll` after a throw: the previous month's results on the first `!roll` of the month's first stream, once (`bot_state.roll_board_month`, then `_announced` in memory); `regalia()` – the strip above the overlay's titles (last month's first places, this month's days and leader), called by `on_stream()` beside `overlay.refresh()`. The month is the one the latest stream started in. BOT.md «Шаг 7а5», «Регалии»
- `texts.py` – pieces shared by `!roll`, reward outcomes and reward descriptions
- `redemption.py` – a redemption's outcome: fulfill, refund or leave alone; the follow gate for a buyer (`_may_play()`); no Twitch calls of its own
- `announce.py` – `curse_lift_loop()`, `on_stream_start()` (the curses of finished streams cleared silently)
- `choke.py` – `choke_loop()`: the choking flask's steps off the rolls while a stream is live, the titles to the overlay when a step moved them
- `perks.py` – perks at stream start and on a player's first message
- `tiers.py` – `on_chat()`: the viewer's status in the stream, written from chat when it changes, for the rewards (a redemption carries no badges)
- `overlay.py` – the game on the stream (BOT.md «Оверлей игры в OBS»): event builders (`thrown()`, `redeemed()`, `lifted()`, `perks()`, `perk_on()`, `board()` – the month's top threes, `regalia()` / `set_regalia()` – the strip above the titles, kept in the state, `wake()` – a game command with no card brings the titles back), `publish()` (never raises or waits; called by `command.py`, `redemption.py`, `announce.py`, `perks.py` beside their chat line; marks the last game event, which the state sends as `quiet`), `refresh()` (titles from the DB at start and on stream start and end); served by the game service on `ROLL_OVERLAY_HOST`:`ROLL_OVERLAY_PORT`, `add_routes()` (`/roll/` serves `overlay.html`, `/roll/events`, `/roll/state`)
- `overlay.html` – the OBS page: cards, titles, the queue, reconnects, `?layout=feed|left|card|hud`, `?scale=`, `?titles=` (minutes the titles stay after the last game event), `?nicks=0`, `?regalia=0`, `?demo=1|mass|board`, `?fx=0`, `?board=` (seconds the month's board plate stays, on a stage of its own opposite the titles); the regalia strip above the titles, set like a title plate, and the crown on the last month's champion; the mass flasks' full-frame effect (canvas particles, ten seconds, drawn only while on screen); its texts are the `TEXT` object at the top of the script

**twitch/roll** – the bot's side of the game service
- `adapter.py` – `RollAdapter`: everything the bot does on Twitch for the game, behind hooks the bot calls – `add_broadcaster_token()`, `attach()` (the redemption listener, the game's commands from `bot_state.roll_commands` while the game is still silent), `start_services()` (the bot's API, the event queue, `_load_commands()` – asks the game until it answers, then keeps the list), `start_watch()` (the rewards watch), `on_ready()`, `on_online()`, `on_offline()`, `on_channel_login()`, `close()`; the rewards' start and its retry (`start_rewards()`, `_retry_rewards()`), a dead channel token (`_rewards_failed()`, `tell_rewards_down()`)
- `client.py` – `RollLink` (the contract; `RollUnavailable` when the game cannot answer, `RollSlow` when it may have done the work), `GameCommand`, `RollClient` over the game's API: the declared commands (`commands()`), commands with the message held for replies (`BotApi.hold()`), the gate's refunds applied (`apply_gate()`), chat and stream events through one ordered queue (`events_loop()`, the oldest dropped when full, a stream event retried while the game restarts), specs, redemptions, `applied()`; `Spec`
- `rewards.py` – `RewardService` (the rewards on Twitch: created and updated from the game's specs, the subscription, statuses, pause, stale redemptions settled by the game's journal; a redemption while the game is down is refunded with `texts.reward_unavailable`, one it was too slow on is looked up in its journal a minute later – `_recheck()`), `RewardComponent`, the `rewards` table's queries (`get_reward_ids()`, `save_reward_id()`)

**discord** (BOT.md «Голос в Discord»)
- `bot.py` – `DiscordBot(discord.Client)`: loads libopus, reads the saved choices (`load_state()`: `bot_state.discord_voice`, `discord_voice_enabled`) and starts the `Speaker` and the minute presence check in `setup_hook`; `Where` (out / here / elsewhere / recovering – discord.py's own reconnect gets 90 s and its own move handling 10 s, checked every 10 s while unsettled – / down) and `presence()` (the pure rule: in the voice channel exactly while `!join` / `!leave` says so, and in that channel – a dragged bot is moved back, and the move is checked, since `move_to()` swallows its timeout), `_reconcile()` on ready, on the bot's own voice state and every minute; the voice client is found among the bot's own connections. `!join` / `!leave` / `!voice` (a toggle, saved) for the owner and `DISCORD_COMMAND_ROLE_IDS` (`may_command()`), answered with a reaction – anyone else gets `texts.discord_no_rights`, once per 30 s per person; `!help` for anyone in the text channel, once per 10 s (both through `Cooldowns`). `DiscordService` – a fresh client after a failed start (30 s doubling to 10 min, reset once a session came up), no retry for a rejected token or a missing intent
- `local/voice/text.py` – `prepare()` / `spoken()`: an answer → text for the Russian TTS model (nicks from `lists.voice_nicks`, words from `lists.voice_words`, emotes even with punctuation stuck to them, links, CAPS, numbers, versions «5.5», `%`, `+`, addressees at the start dropped, `trim_to_sentence()`); `sentences()` – the parts for `VOICE_SENTENCES`
- `local/voice/stress.py` – `load()` (ruaccent in tiny mode, once per process, telemetry off) / `apply()`: ё (`VOICE_YO`) and every stress as an upper-case vowel (`VOICE_STRESS`), carried back onto the text word by word; a word already marked by `lists.voice_words` is kept
- `local/voice/tts.py` – `TTSClient.stream()`: streaming PCM from `/v1/audio/speech` with `language: Russian` (5 s to connect, 100 s to the first byte, 30 s of silence after it, the whole answer bounded by the caller), `TTSUnavailable`
- `local/voice/audio.py` – `TimeStretch` (`VOICE_TEMPO` without a pitch change, WSOLA in numpy, independent of chunk borders; `flush()` once the server is done), `Upsampler` (24 kHz mono → 48 kHz stereo across chunk borders), `StreamSource` (a dry buffer mid-answer makes one pause until 1.5 s are back, each next pause in the answer gathering twice as much up to 6 s – no crackle; end only after `finish()`)
- `local/voice/speaker.py` – `Speaker`: `submit()` (the speech listener: queue or drop), `run()` (one answer at a time; an answer queued longer than `VOICE_MAX_WAIT_SECONDS` is dropped; ruaccent loads beside it), `speak()` (ё and stresses per part, the parts one request each with 0.15 s between them, prebuffer by the answer's expected length and the server speed measured on earlier answers – `prebuffer_seconds()`, `_measure()` – then play, pauses and player errors logged; a playback stopped early cancels the synthesis, a stuck one is stopped after `2 × VOICE_TIMEOUT`)

**cli**
- `main.py` – argparse, `_with_db`, `_check_combination()` (one command per run, modifiers only with their command)
- `knowledge.py` – lore import in three formats, `clear_knowledge()`, `lore_sources()`
- `memory.py` – `--build-memory`
- `probe.py` – `--probe-context`: contexts (`now` / `new`) or generation settings (`Candidate`: `bot` / `candidate`)
- `emotes.py` – `--sync-emotes`

### `CONTENT.md` – everything the bot says

One Markdown file: `## section` → `### key` → the value until the next heading. Sections `prompts` (`Content.prompt()`), `labels` (context headings the model sees; `prompts.system` names them, so they stay in sync), `texts` (every reply in chat), `lists` (`emotes`, `follow`, `banned`, `voice_nicks`, `voice_words`, `months`, `weekdays`, one entry per line), `channel` (the channel's own commands: `### !tg` and its reply; keys are free, not in `REQUIRED`). Re-read when its mtime changes, no restart. `.env` holds secrets, numbers and flags; `CONTENT.md` holds text – nothing the chat sees is hardcoded. Placeholders go through `safe_format()`. `<!-- … -->` and prose before the first `###` are notes. A duplicate key logs an error; a file that parses to nothing or lacks a `REQUIRED` key keeps the previous version; `validate_content()` stops the start on a missing key. BOT.md «CONTENT.md – тексты бота»

## Invariants that are easy to break

Each is explained in `BOT.md` or `README.md`; this is the list to keep in mind while editing code.

- **Database.** Every write goes through `async with transaction()` (a nested one joins the outer); never `get_db()` + `commit()`. A script that touches the DB ends with `close_db()`, or its aiosqlite thread keeps the process alive. `init_db()` is walked on every start – in `run_bot()`, before either platform starts, since both write, in the game service, and by every CLI command, against the live database while the old bot still runs: steps are idempotent and additive, and the walk is one `BEGIN IMMEDIATE` transaction, so processes starting together take turns. All queries share one connection, so `gather` over them gives order, not parallelism
- **Commands are not stored** in `chat_messages`; readers still filter old `!…` rows. `has_chatted()` also looks at `rolls` and `bot_uses`
- **The gate.** Help, channel commands and `!rollboard` (`public`) skip the personal cooldown and take a shared per-trigger one instead (`COOLDOWN_PUBLIC`). The cooldown is set right after its check with no `await` in between (twitchio runs every event in its own task); a quota refusal gives it back; a handler rejecting malformed input calls `ctx.refuse()` (cooldown and quota row) or `ctx.clear_cooldown()`
- **Session = stream**; `session_id` is read once per event, since coroutines outlive a switch
- **Game state** changes only in `src/roll/game.py`, under its lock, one transaction with its journal row. The lock is per process: only the game service runs the game
- **Services.** The game never imports `src/twitch`, the bot never imports `src/roll` (but for the memory's read). The game reaches Twitch only through the bot's API; a new need is a new route there and a member of `RollPort`, never a token in the game. Every bot → game call carries the stream state. A game command's replies go out while the call runs (`/reply` finds the held message), so the bot keeps the message held until the call returns
- **twitchio 3.x.** Its command system is off (`process_commands()` is a no-op). `CustomRewardRedemption.fulfill()` sends the wrong id – statuses go through `_http.patch_custom_reward_redemption()`. A reply carries `parent_user`, not `parent_user_id`. Private fields read by the chat watch and the token check are verified at start. On `session_reconnect` twitchio drops the live socket from its registry unless `keep_migrated_sockets()` ran; the watch then subscribes twice. After a reconnect twitchio may keep a socket open with its subscriptions lost, or stop reconnecting it at all: a socket counts as alive only while it is connected and holds the watched subscription, or has a reconnect running. `event_ready` fires once per start: nothing in it may raise. `event_message` drops a message id it has already seen. Signal handlers are installed again from `event_ready`, because the OAuth adapter's aiohttp replaces them
- **Gemini.** Every config comes from `make_gen_config()` (a hand-built one loses `thinking_config`). Chat answers go through `generate()` or `ladder.walk()`, which keep to `GEMINI_ANSWER_DEADLINE`; the memory calls `generate_checked()` directly; memory, the pulse and the fact check hold `client.background()` around their requests, so together they never take more than two of the chat's slots; its config also comes from `make_gen_config()`, so `GEMINI_THINKING_BUDGET` covers it. Safety filters are off for the persona and on only for the `!ascii` check. A timeout is not retried
- **Output.** Every line the bot sends on its own goes through `Bot.send_chat_message()`, which runs `defuse()`; every reply to a viewer goes through `reply()`, which logs a send failure instead of raising; answers go through `cleanup_response()`. `strip_markdown()` keeps `_` (it is part of nicks)
- **Follow gate.** `bool(await followers.followers)` – the iterator itself is always truthy. The cache fails open
- **Case.** Free text keeps its case; FTS queries are lowercased (uppercase `AND`/`OR`/`NOT` are operators); Cyrillic facts are matched with `casefold()` in Python, since SQLite folds only ASCII
- **Voice.** Only an answer that reached chat goes to `speech.say()`; refusals and service lines are not voiced. `Speaker.submit()` never blocks the chat path: it drops an answer rather than queue it past `VOICE_QUEUE` or while the bot is out of the voice channel, and the worker drops one that waited past `VOICE_MAX_WAIT_SECONDS`. The TTS server's own normalization stays off (`normalize: false`): it is English and garbles Russian. A stress goes to the model as an upper-case vowel: it reads `+` as «плюс», and an acute accent garbles the word. `StreamSource.read()` returns `b''` only after `finish()` and an empty buffer – an early `b''` ends playback mid-answer
- **Tests** (`tests/`, `make test`) clear every variable of `.env.example` and set the environment in `conftest.py` before anything from `src` is imported, recreate module-level asyncio primitives and caches per test, and replace `CONTENT.md` with one whose values are the key names. `client.get_client()` raises: a Gemini stub is patched where it is used (`ladder`, `commands`, `proactive`, `picture.command`, `memory.build`, `pulse`, `fact_rest`). A new module-level lock, semaphore or cache is added to `_isolation` in `conftest.py`
- **Deployment.** The tree is mounted read-only at `/app`, dependencies live in `/deps`, the image is distroless (no shell; libopus is copied in from the builder and found through `DISCORD_OPUS_LIB`; ruaccent's models are downloaded in the builder, pruned to what its tiny mode reads, and found through `BOT_ACCENT_PATH`, with `HF_HUB_OFFLINE=1`), two ports are published – the mascot's page (bot, `MASCOT_PUBLISH`) and the game's overlay (roll, `ROLL_PUBLISH`), 127.0.0.1 unless set – never the OAuth one or the services' internal APIs, which stay on the compose network; the game service runs from the same image with `/app/roll.py`; new tokens come from `make oauth`, logs go to stdout. Both tokens are read from `.tio.tokens.json` first; the `.env` values are only a fallback, since adding them on top would undo a new login. Session ids, memory keys and log times use `BOT_TIMEZONE`, not the process zone; `TZ` is not set anywhere. README «Эксплуатация»

## Environment Variables

Required: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_BOT_ID`, `TWITCH_CHANNEL`, `GEMINI_API_KEY`. `.env.example` is the authoritative list (188 variables, grouped by section, each commented; `MASCOT_PUBLISH` and `ROLL_PUBLISH` are read by compose, not by the services) and `README.md` has the table; keep both in sync with `src/core/config.py`. **No text belongs here** – it goes to `CONTENT.md`.

## When changing things

- **Dash: only `–` (en dash, U+2013).** The em dash (U+2014, deliberately not typed here) is not used anywhere in this project: not in `CONTENT.md`, `.env.example`, the docs, code comments or docstrings, and not in replies. Never add one. Check with `grep -rlP '\x{2014}'` over tracked files. `−` (U+2212) stays where it means minus, e.g. `75 − STEP`
- **Code comments and docstrings are in English.** Log messages, chat texts and the docs (`*.md`, `CONTENT.md`, `.env.example`) are in Russian. Russian chat words used as data examples (сосурян, «залупа стрима», НЕЛЬЗЯ/МОЖНО) stay as they are inside English comments
- **Comments and docs state the current state, not its history.** This applies to every comment, docstring, `<!-- … -->` note and line of documentation in the project.
  - **Forbidden:** dates (`2026-09-20`), attribution (`the owner asked`, `owner, 2026-09-19`, `found in review`, `at the owner's request`), change history (`used to be`, `was replaced on`, `before the fix`, `added on`), narrative of how a decision was reached, first person, rhetorical questions, conversational asides and jargon.
  - **Required:** the constraint, the invariant or the failure mode that makes the code what it is. A number that justifies a constant stays as a bare number, without who measured it or when.
  - **Length:** two or three lines for a comment. A docstring is one summary line, plus at most a short paragraph of constraints. Anything longer belongs in the docs, not next to the code.
  - Rewrite instead of deleting: `# The owner asked on 2026-09-15 to lower this from 5 to 3, because…` becomes `# Three throws: beyond that the points economy takes over`.
  - Rationale that is genuinely long belongs in `BOT.md` as a statement of how the thing works, still without the history of how it got there.
- **Docs are part of the change, not a follow-up.** Every change to behaviour, config, schema or layout updates the affected docs in the *same* turn, before reporting the work as done. A change that is only documented in the commit message is unfinished. Each fact lives in one document (see "Documentation"): how a mechanic works – `BOT.md`; what the bot can do, installation, operations, the command and variable tables – `README.md`; layout, module map and these rules – `CLAUDE.md`; plus `.env.example` and the `<!-- … -->` notes in `CONTENT.md`. Do not retell a mechanic in a second document – link to it
- Adding a channel command (a link, an info line): only `### !name` and its reply in `## channel` of `CONTENT.md`, no code and no restart
- Adding a command: one `add()` line in `ChatComponent.__init__` + a handler in the right package (see "Where code goes") + its texts in `## texts` of `CONTENT.md` and in `REQUIRED` (`src/core/content.py`). A game command: its handler in `src/roll/` on a `Request` and a `Command` in `COMMANDS` of `src/roll/service.py`; the bot registers what the game declares (`ChatComponent.set_game_commands()`), no bot change. Then: `README.md` (command table), `BOT.md` (registry table and its own «Шаг 7…» section), `CLAUDE.md` only if a module was added
- Adding config: `src/core/config.py` (with a validated `_env_*` helper) + `.env.example` + README env table. If it is a *text*, it is not config – put it in `CONTENT.md`. A default shown on the bot map (`docs/index.html`: cooldowns, quotas, limits, timers) is written there by hand – change it there too
- Adding a text: `CONTENT.md` + `REQUIRED` in `src/core/content.py`. Never inline a chat-visible string in a handler
- Editing `CONTENT.md` while the bot runs: the running process hot-reloads it and renders it with **its own, old** code. Adding a placeholder to an existing key before the restart makes `safe_format()` fail and the raw template (`@{user} выбил {value}…`) goes to chat. Add a new key instead
- Changing rewards: title/prompt in `CONTENT.md`, cost/limit in `.env` – both reach Twitch only on restart. Never create the rewards by hand in the dashboard
- Changing the DB schema: `init_db()` must stay idempotent – it runs on every start against a live 90-session database. It is a list of named steps, `STEPS` in `src/core/db/schema.py`, walked in order; a change is a new step at the end (or an edit inside the step that owns the table) that checks for itself whether it is already done. `PRAGMA user_version` holds `len(STEPS)` and is written with the steps, and a step that fails is named in the log
