# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository: the rules, the layout and the invariants. How each mechanic works is in `docs/BOT.md`.

## Project Overview

Twitch chat bot powered by Gemini 2.5 Flash. Commands (`!who`, `!ask`, …) work as plain chat text. Free text is answered only when it contains a `сосур*` / `secur*` word (сосурян, сосурити, securityexpert, etc.), an `@botname` mention, or is a reply to a bot message. Stores full chat history in SQLite with FTS5 search and long-term memory.

## Documentation

| File | Holds | Language |
|---|---|---|
| `README.md` | what the bot can do, installation, operations, CLI, the command and environment variable tables, project tree | Russian |
| `docs/BOT.md` | the one detailed reference: every mechanic, its numbers and the failure modes it guards against | Russian |
| `CLAUDE.md` | this file: rules, layout, module map, invariants | English |
| `docs/CONTENT.md` | everything the bot says, read by the code; `<!-- … -->` notes explain keys | Russian |
| `.env.example` | every environment variable with a comment | Russian |

`BOT.md` and `CONTENT.md` below mean the files in `docs/`.

Before changing a feature, read its section in `BOT.md`.

## Commands

```bash
# Run in production: Docker Compose. The image carries the dependencies; the working
# tree, mounted read-only at /app, is the code that runs (a restart runs the checked-out
# branch); the database and tokens live on a volume
docker compose up -d
docker compose logs -f
docker compose restart bot

# A maintenance command runs in the same image. Paths are the container's: /app is the
# tree (read-only), /data holds the database and the tokens
docker compose run --rm bot /app/bot.py --backup /data/copy.db

# New Twitch tokens (bot or channel account): the container publishes no port
make oauth

# Run without a container (debugging). Always use venv python, not system python;
# from data/ it finds the container's .tio.tokens.json; the DB path is explicit, the default is the repo root
cd data && BOT_DB_PATH=chat_history.db ../venv/bin/python3 ../bot.py

# Upload lore from txt files (bot doesn't start)
./venv/bin/python3 bot.py --upload-lore lore.txt [file2.txt ...]

# Preview without writing to DB
./venv/bin/python3 bot.py --upload-lore lore.txt --dry-run

# Clear knowledge and re-import
./venv/bin/python3 bot.py --upload-lore lore.txt --clear-lore

# Clear knowledge only (no import) – everything, or one source; with --dry-run only counts
./venv/bin/python3 bot.py --clear-lore
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
make audit           # known vulnerabilities in requirements.txt and requirements-dev.txt
make lock            # recompile requirements*.txt after editing requirements*.in
```

## Architecture

Entry point is thin; all logic lives in `src/`, split into packages by purpose.

### Where code goes

| Package | Holds | Put here |
|---|---|---|
| `src/core/` | skeleton: config, texts, DB, logging, utils, command registry, chat dispatcher | nothing feature-specific. Features depend on core, never the reverse – the one exception is `component.py`, which registers every feature's commands |
| `src/gemini/` | everything that costs a Gemini call (incl. `picture/` – `!ascii`) | a command that generates text (`kind=Kind.GEMINI`), anything that builds prompts |
| `src/local/` | сосурян's own features without Gemini: simple commands, follow replies, emote spam, the periodic command reminder, and the `roll/` game | a small command or event reply served from SQLite + `CONTENT.md` |
| `src/local/roll/` | the «залупа стрима» game: throws, channel-points rewards, perks, curse-lift announcements. A local feature that grew big enough for its own subpackage | anything about `!roll` |
| `src/cli/` | `bot.py --…` commands; the bot does not start | a maintenance command |

A feature that grows its own tables, texts and background loops gets **its own subpackage inside the package of its kind** – `local/<feature>/` like `local/roll/`, or `gemini/<feature>/` if it generates text – instead of spreading across several files of `local/` and `core/`: roll is a local feature of the bot, not a top-level one. Table schema and migrations still go into `init_db()` in `src/core/db/schema.py` (one place to keep startup idempotent); the feature's queries live in its package, like `src/local/roll/storage.py`. Each package's `__init__.py` lists its modules. Paths to `CONTENT.md` and `chat_history.db` live in `src/core/paths.py`: they default to `docs/CONTENT.md` and `chat_history.db` in the repository root, found via `Path(__file__).parents[2]`, and `BOT_DB_PATH` / `BOT_CONTENT_PATH` move them – that is how the container keeps its data on a volume instead of inside the image. `db/connection.py` and `content.py` import `DB_PATH` / `CONTENT_PATH` under their own names, because callers import `get_db()` rather than the path, and the tests patch them there.

### Modules

One line per module: what it holds. How a feature behaves and why lives in `BOT.md`; the references below name its section.

- `bot.py` – `Bot(commands.Bot)`, the assembly point: lifecycle (`setup_hook`, `event_ready`, `event_oauth_authorized`, `close()`), EventSub subscriptions, `send_chat_message()`, the stream state (`stream`, `session_id`, `stream_went_online()` / `_offline()`, `_sync_stream()`), background task and reward start, `run_bot()`. `__main__` delegates to `src/cli/main.py`. BOT.md «Жизненный цикл бота»

**core**
- `component.py` – `ChatComponent`: the registry of all commands, `event_message`, the pure `route()` and the gate `_gate()` (follow, cooldown, role, quota), `event_follow`. BOT.md «Обработка сообщений»
- `commands.py` – `CommandContext`, `CommandEntry`, `CommandRegistry`, `Kind` and `Role` (`StrEnum`: their values are the plain strings stored in cooldown keys and `bot_uses.kind`)
- `config.py` – every environment variable, parsed and validated (`_env_int`, `_env_float`, `_env_bool`, `_env_percent`, `_interval_range()`), in classes `Files`, `Clock`, `Logging`, `Twitch`, `Gemini`, `Chat`, `Caps`, `Cooldown`, `Quota`, `Follow`, `Summary`, `Who`, `Picture`, `Stream`, `Roll`, `Rewards`, `Context`, `Memory`, `Help`, `Proactive`, `Emote`
- `paths.py` – `DB_PATH`, `CONTENT_PATH`: `chat_history.db` in the repository root and `docs/CONTENT.md`, or `BOT_DB_PATH` / `BOT_CONTENT_PATH`
- `content.py` – `CONTENT.md` access (`Content.prompt/label/text/items`), mtime cache, `REQUIRED`, `validate_content()`
- `database.py` – facade re-exporting `src/core/db/`: callers import every query from here
- `db/connection.py` – the shared connection (`get_db()`, `close_db()`, `reopen()`), `transaction()`, `backup_db()`, `vacuum_db()`
- `db/schema.py` – `init_db()`: named `STEPS` for every table, `SCHEMA_VERSION`
- `db/chat.py` – `chat_messages`: saving, chat windows, previous/last session (cached), `!stat` numbers
- `db/interactions.py` – `bot_interactions`: tagged answers, the viewer's dialogues
- `db/knowledge.py` – `knowledge` and `facts`: FTS5 search, the random «language» sample
- `db/quota.py` – `bot_uses`: hourly quota, channel ceiling, per-stream counts
- `db/streams.py` – `streams`: stream id → session, start, end
- `activity.py` – `ChatWatch`: «has anyone written since» for the chat loops
- `port.py` – `BotPort` / `StreamBot`: what features need from the bot. Core and the features never import `bot.py`; `tests/test_bot.py` checks that both `Bot` and `tests/fakes.py`'s `FakeBot` have every member
- `viewer.py` – `Tier`, `tier_of()`, `by_tier()`: the one status ladder behind every per-status number
- `followers.py` – `FollowerCache`: follower check via Helix with a TTL, fails open
- `cooldowns.py` – `Cooldowns` on the monotonic clock
- `tasks.py` – `BackgroundTasks`: loops by name, never started twice
- `chat_socket.py` – `ChatSocketWatch` (resubscribes when the chat socket is gone), `check_private_api()`, `keep_migrated_sockets()` (keeps a migrated socket in twitchio's registry)
- `tokens.py` – the channel token for rewards, token saving, OAuth links, `token_problem()`
- `heartbeat.py` – `heartbeat_loop()` for the container healthcheck
- `stream.py` – `StreamTracker` (session = stream, resume rules), `watch_stream()`, `fetch_live_stream()`, `end_from_vod()`. BOT.md «Сессии»
- `logging_setup.py` – `setup_logging()`
- `utils.py` – shared helpers: `SOSUR_RE`, `clean_nick`, `safe_format`, `defuse`, `reply_to_bot`, `local_time`, `random_delay`, `gather_cancelling`. `SOSUR_RE` lives here rather than in `component.py`: `db/schema.py` backfills `chat_messages.addressed` with it, and importing the dispatcher there would be circular

**gemini**
- `client.py` – `get_client()`, `generate()` (chat, with the answer deadline), `generate_checked()` (the reason there is no text), `make_gen_config()`, `SAFETY_OFF` / `SAFETY_CHECK`, `usage`, `cost_estimate()` (the one place the Gemini prices live). BOT.md «Шаг 10: Вызов Gemini»
- `context.py` – `ContextBuilder`, `chat_chars()`, `tail_within()`
- `ladder.py` – `Rung`, `walk()` (the fallback ladder), `unique_rungs()`
- `answer_context.py` – free-text context: `Question`, `ladder()`, `answer()`; shared with `--probe-context`. BOT.md «Контекст Gemini»
- `output.py` – cleanup of an answer before chat: `cleanup_response()`, `trim_to_sentence()`, `split_into_chunks()`, `strip_*`, `fix_dashes()`, `TWITCH_MSG_MAX`. BOT.md «Шаг 11»
- `responder.py` – `respond_and_save()`, `send_chunked()`, moderation, CAPS, emote
- `commands.py` – handlers of free text, `!ask`, `!summary`, `!who`, `!versus`; `LIMITS`
- `limits.py` – `PerStreamLimit`: per-stream limit, one call per viewer at a time, count only what reached chat
- `summary.py`, `who.py` – the context of `!summary` and of `!who` / `!versus`
- `proactive.py` – `proactive_loop()`
- `memory/storage.py`, `memory/build.py` – long-term memory: chronicles, events, profiles, `memory_loop()`. BOT.md «Память бота»
- `picture/fetch.py`, `picture/render.py`, `picture/command.py` – `!ascii`: safe download, braille rendering, the handler with its cache. BOT.md «Шаг 7ж»

**local**
- `commands.py` – `handle_help`, `handle_stats`
- `follow.py` – `handle_follow()`
- `emote_spam.py` – `emote_spam_loop()`
- `help_announce.py` – `help_loop()`, `note_help_shown()`

**local/roll** – the «залупа стрима» game (BOT.md «Шаг 7а», «Награды за баллы канала»)
- `rules.py` – the rules as pure functions of `now`
- `game.py` – the only code that mutates `rolls`, under one lock: `free_throw()`, `redeem()`, `lift_expired_curses()`, `status()`, perks
- `storage.py` – queries on `rolls`, `rewards`, `roll_actions`, `roll_perks`
- `command.py` – `handle_roll`, `handle_rollstat`
- `texts.py` – pieces shared by `!roll`, reward outcomes and reward descriptions
- `redemption.py` – a redemption's outcome: fulfill, refund or leave alone; no Twitch calls
- `rewards.py` – `RewardService` (Twitch side of the rewards) and `RewardComponent`
- `announce.py` – `curse_lift_loop()`
- `perks.py` – perks at stream start and on a player's first message

**cli**
- `main.py` – argparse, `_with_db`, `_check_combination()` (one command per run, modifiers only with their command)
- `knowledge.py` – lore import in three formats, `clear_knowledge()`, `lore_sources()`
- `memory.py` – `--build-memory`
- `probe.py` – `--probe-context`
- `emotes.py` – `--sync-emotes`

### `CONTENT.md` – everything the bot says

One Markdown file: `## section` → `### key` → the value until the next heading. Sections `prompts` (`Content.prompt()`), `labels` (context headings the model sees; `prompts.system` names them, so they stay in sync), `texts` (every reply in chat), `lists` (`emotes`, `follow`, `banned`, one entry per line). Re-read when its mtime changes, no restart. `.env` holds secrets, numbers and flags; `CONTENT.md` holds text – nothing the chat sees is hardcoded. Placeholders go through `safe_format()`. `<!-- … -->` and prose before the first `###` are notes. A duplicate key logs an error; a file that parses to nothing or lacks a `REQUIRED` key keeps the previous version; `validate_content()` stops the start on a missing key. BOT.md «CONTENT.md – тексты бота»

## Invariants that are easy to break

Each is explained in `BOT.md` or `README.md`; this is the list to keep in mind while editing code.

- **Database.** Every write goes through `async with transaction()` (a nested one joins the outer); never `get_db()` + `commit()`. A script that touches the DB ends with `close_db()`, or its aiosqlite thread keeps the process alive. `init_db()` is walked on every start and by every CLI command, against the live database while the old bot still runs: steps are idempotent and additive. All queries share one connection, so `gather` over them gives order, not parallelism
- **Commands are not stored** in `chat_messages`; readers still filter old `!…` rows. `has_chatted()` also looks at `rolls` and `bot_uses`
- **The gate.** The cooldown is set right after its check with no `await` in between (twitchio runs every event in its own task); a quota refusal gives it back; a handler rejecting malformed input calls `ctx.refuse()` (cooldown and quota row) or `ctx.clear_cooldown()`. Roles are checked on badges, not on `tier_of()`
- **Session = stream**; `session_id` is read once per event, since coroutines outlive a switch
- **Game state** changes only in `local/roll/game.py`, under its lock, one transaction with its journal row
- **twitchio 3.x.** Its command system is off (`process_commands()` is a no-op). `CustomRewardRedemption.fulfill()` sends the wrong id – statuses go through `_http.patch_custom_reward_redemption()`. A reply carries `parent_user`, not `parent_user_id`. Private fields read by the chat watch and the token check are verified at start. On `session_reconnect` twitchio drops the live socket from its registry unless `keep_migrated_sockets()` ran; the watch then subscribes twice. `event_message` drops a message id it has already seen. Signal handlers are installed again from `event_ready`, because the OAuth adapter's aiohttp replaces them
- **Gemini.** Every config comes from `make_gen_config()` (a hand-built one loses `thinking_config`). Chat answers go through `generate()` or `ladder.walk()`, which keep to `GEMINI_ANSWER_DEADLINE`; the memory calls `generate_checked()` directly and takes its own slots. Safety filters are off for the persona and on only for the `!ascii` check. A timeout is not retried
- **Output.** Every line the bot sends on its own goes through `Bot.send_chat_message()`, which runs `defuse()`; answers go through `cleanup_response()`. `strip_markdown()` keeps `_` (it is part of nicks)
- **Follow gate.** `bool(await followers.followers)` – the iterator itself is always truthy. The cache fails open
- **Case.** Free text keeps its case; FTS queries are lowercased (uppercase `AND`/`OR`/`NOT` are operators); Cyrillic facts are matched with `casefold()` in Python, since SQLite folds only ASCII
- **Tests** (`tests/`, `make test`) set the environment in `conftest.py` before anything from `src` is imported, recreate module-level asyncio primitives per test, and replace `CONTENT.md` with one whose values are the key names. `client.get_client()` raises: a Gemini stub is patched where it is used (`ladder`, `commands`, `proactive`, `picture.command`, `memory.build`). A new module-level lock, semaphore or cache is added to `_isolation` in `conftest.py`
- **Deployment.** The tree is mounted read-only at `/app`, dependencies live in `/deps`, the image is distroless (no shell), no port is published, new tokens come from `make oauth`, logs go to stdout. Session ids and memory keys use `BOT_TIMEZONE`, not the process zone. README «Эксплуатация»

## Environment Variables

Required: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_BOT_ID`, `TWITCH_CHANNEL`, `GEMINI_API_KEY`. `.env.example` is the authoritative list (102 variables, grouped by section, each commented) and `README.md` has the table; keep both in sync with `src/core/config.py`. **No text belongs here** – it goes to `CONTENT.md`.

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
- Adding a command: one `add()` line in `ChatComponent.__init__` + a handler in the right package (see "Where code goes") + its texts in `## texts` of `CONTENT.md` and in `REQUIRED` (`src/core/content.py`). Then: `README.md` (command table), `BOT.md` (registry table and its own «Шаг 7…» section), `CLAUDE.md` only if a module was added
- Adding config: `src/core/config.py` (with a validated `_env_*` helper) + `.env.example` + README env table. If it is a *text*, it is not config – put it in `CONTENT.md`
- Adding a text: `CONTENT.md` + `REQUIRED` in `src/core/content.py`. Never inline a chat-visible string in a handler
- Editing `CONTENT.md` while the bot runs: the running process hot-reloads it and renders it with **its own, old** code. Adding a placeholder to an existing key before the restart makes `safe_format()` fail and the raw template (`@{user} выбил {value}…`) goes to chat. Add a new key instead
- Changing rewards: title/prompt in `CONTENT.md`, cost/limit in `.env` – both reach Twitch only on restart. Never create the rewards by hand in the dashboard
- Changing the DB schema: `init_db()` must stay idempotent – it runs on every start against a live 90-session database. It is a list of named steps, `STEPS` in `src/core/db/schema.py`, walked in order; a change is a new step at the end (or an edit inside the step that owns the table) that checks for itself whether it is already done. `PRAGMA user_version` holds `len(STEPS)` and is written with the steps, and a step that fails is named in the log
