# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Twitch chat bot powered by Gemini 2.5 Flash. Responds to `сосур*` words (сосурян, сосурити, etc.), `@botname` mentions, and replies to bot messages. Stores full chat history in SQLite with FTS5 search and long-term memory.

## Commands

```bash
# Run (always use venv python, not system python)
./venv/bin/python3 bot.py

# Upload lore from txt files (bot doesn't start)
./venv/bin/python3 bot.py --upload-lore lore.txt [file2.txt ...]

# Preview without writing to DB
./venv/bin/python3 bot.py --upload-lore lore.txt --dry-run

# Clear knowledge and re-import
./venv/bin/python3 bot.py --upload-lore lore.txt --clear-lore

# Clear knowledge only (no import)
./venv/bin/python3 bot.py --clear-lore

# List all saved facts
./venv/bin/python3 bot.py --list-facts

# Lore import runs init_db() automatically — no bot restart needed.
# Logic in src/knowledge.py. See BOT.md for detailed lore file format guide.

# Setup venv from scratch
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Architecture

- `bot.py` — `commands.Bot` subclass + `ChatComponent` (handles `event_message`, `event_follow`). Proactive message loop. Entry point (`__main__` delegates to `src/cli.py` for CLI commands, falls back to `run_bot()`)
- `src/cli.py` — CLI: argparse, `--upload-lore`, `--clear-lore`, `--dry-run`, `--list-facts`. `main()` returns `False` if no CLI args (bot should start)
- `src/config.py` — config classes: `Twitch`, `Gemini`, `Caps`, `Cooldown`, `Context`, `Proactive`. System prompt loaded from `prompt.txt` via `Gemini.get_system_instruction()`
- `src/database.py` — SQLite async (aiosqlite): shared connection via `get_db()`/`close_db()`, init, save, query, FTS5 search. FTS tables use `content=` with auto-sync triggers. Indexes on `session_id` for `chat_messages` and `bot_interactions`
- `src/commands.py` — `CommandContext` (dataclass: message, user, prompt, original_text, session_id, bot), `CommandEntry`, `CommandRegistry`. Registry populated in `ChatComponent.__init__`; `resolve(prompt)` returns matching entry by exact or prefix match
- `src/context.py` — `ContextBuilder`: assembles Gemini prompts from named sections. Used in `_handle_default`, `_handle_who`, `_handle_versus`, `_proactive_loop`. `build()` renders full prompt; `build_without(*labels)` renders fallback without specified sections
- `src/gemini.py` — Gemini client (`get_client()`), `generate()`, `make_gen_config()`, `SAFETY_OFF` constants. `_semaphore` limits to 5 concurrent requests; `asyncio.wait_for(..., timeout=60)` prevents hanging calls
- `src/knowledge.py` — knowledge base operations: parse lore txt files, import entries, clear knowledge
- `src/utils.py` — shared utilities: `is_caps`, `caps_preserve_mentions`, `strip_markdown`, `split_into_chunks`, `cleanup_response`, Twitch message limit constants (`TWITCH_MSG_MAX`, `WHO_VERSUS_MAX`, `CHUNK_SEND_DELAY`)
- `prompt.txt` — system prompt for Gemini (bot personality). Read on every Gemini call — editable without restart

Flow:
1. `setup_hook` — `init_db()` (opens shared DB connection), loads bot token from env, fetches broadcaster ID, registers `ChatComponent`
2. `event_ready` — fetches bot username, calls `_subscribe_to_chat()` (chat + follow events); on failure logs error + prints OAuth URL. Starts `_proactive_loop` if `PROACTIVE_ENABLED` (reconnect guard prevents duplicate tasks)
3. `event_oauth_authorized` — saves token, prints `TWITCH_BOT_TOKEN` / `TWITCH_BOT_REFRESH` to console
4. `ChatComponent.event_message`:
   - Computes `session_id` once as local variable (prevents race condition at midnight)
   - Saves every non-bot message to `chat_messages` (FTS synced via trigger)
   - Triggers on: `сосур\w*` regex match (SOSUR_RE) OR `@botname` mention OR reply to bot message
   - Checks per-user cooldown (broadcaster exempt)
   - Builds `CommandContext(message, user, prompt, original_text, session_id, bot)`
   - Calls `CommandRegistry.resolve(prompt)` → finds matching `CommandEntry` by exact or prefix match
   - If entry found and `role='vip_mod_broadcaster'` — checks `chatter.vip/moderator/broadcaster`; denies if not privileged
   - Calls `entry.handler(ctx)` and returns
   - Falls through to `_handle_default(ctx)` if no command matched
   - `!help` — sets regular cooldown, replies with command list (no Gemini)
   - `!stat` — sets regular cooldown, queries session + total stats (no Gemini)
   - `!summary` — fetches up to 500 session messages, sends to Gemini with `prompt.txt` + summary overlay (temperature 1.2). Split into up to 3 chunks. Saved with `[summary]` prefix
   - `!who <ник>` — fetches target's facts + messages + past interactions. Prompt via `ContextBuilder`. Saved with `[who]` prefix
   - `!versus <ник1> <ник2>` — parallel fetch for both users. Prompt via `ContextBuilder`. Saved with `[versus]` prefix
   - `!defact` — role-gated. Finds facts by substring, deletes single match or shows list if ambiguous (no Gemini)
   - `!fact` — role-gated. Saves fact to DB (no Gemini)
   - `!ask` — factual mode without `prompt.txt` or context. Plain text, markdown stripped, up to 3 chunks. Saved with `[ask]` prefix
   - Default — sets regular cooldown, fetches context in parallel (facts + chat + FTS + random knowledge), builds prompt via `ContextBuilder`, calls Gemini, responds and saves
5. `ChatComponent.event_follow`:
   - Triggered on new channel follows (EventSub `channel.follow`)
   - Responds with random message from `FOLLOW_MESSAGES` (3 hardcoded templates, no Gemini call)
   - Saves to `bot_interactions` with `[follow]` as user_message
   - Requires `moderator:read:followers` scope (bot is moderator)
6. `Bot._proactive_loop`:
   - Background asyncio task, started in `event_ready` if `PROACTIVE_ENABLED=true`. Reconnect guard prevents duplicate tasks. Task reference in `Bot._proactive_task`
   - Initial delay = `PROACTIVE_INTERVAL_MINUTES`, then repeats every interval
   - Skips if no recent chat messages (empty channel)
   - 50% chance: targets random active user from last 20 messages, 50%: general comment
   - Uses full context from env vars (`Context.CHAT_MESSAGES`, `Context.KNOWLEDGE_RANDOM`)
   - CAPS with `CAPS_PROBABILITY` chance (preserves mentions)
   - Sends via `_send_chat_message()` (HTTP API, no reply context)
   - Saved to `bot_interactions` with `_proactive_` as username

## Database (chat_history.db)

Six tables:
- `chat_messages` — all non-bot chat messages with `session_id`
- `bot_interactions` — bot Q&A pairs with `session_id`
- `facts` — persistent facts saved via `!fact` command, removable via `!defact` (UNIQUE constraint on username+fact, INSERT OR IGNORE)
- `knowledge` — manually imported lore, memes, stream history (unique per content)
- `chat_fts` — FTS5 virtual table linked to `chat_messages` via `content=` (synced by triggers)
- `knowledge_fts` — FTS5 virtual table linked to `knowledge` via `content=` (synced by triggers)

Session ID = current date (`YYYY-MM-DD`) via `Bot.session_id` property (recomputed on each access). All bot restarts on the same day share the same session. Changes automatically at midnight, even without restart.

Context sent to Gemini (in order):
1. `[Сохранённые факты]` — asking user's own facts (always) + other users' facts only if LIKE-match to prompt
2. `[Последние сообщения в чате]` — sliding window of recent chat (non-bot messages only, current session)
3. `[Контекст канала]` — FTS5 search across `knowledge` + `chat_messages` (all-time), combined results
4. `[Язык чата]` — random sample from `knowledge` (always present, regardless of query match)
5. `{user} спрашивает: {prompt}`

## Key Notes

- **twitchio 3.x** — EventSub (WebSocket), not IRC. Requires `client_id` + `client_secret` from dev.twitch.tv.
- **Bot token bootstrap** — if `TWITCH_BOT_TOKEN` + `TWITCH_BOT_REFRESH` set in `.env`, loaded in `setup_hook` via `add_token()`. First-time users do OAuth once to get values printed to console.
- **channel:bot not needed** — bot is a moderator in the channel (`/mod botname`).
- **TWITCH_CHANNEL** — streamer's channel name, not the bot's own channel.
- **Bot personality** — `prompt.txt` in project root. Read on every Gemini call via `Gemini.get_system_instruction()` → `_load_prompt()`. Editable without bot restart (hot-reload).
- **Tokens** — `TWITCH_BOT_TOKEN` auto-refreshes via `TWITCH_BOT_REFRESH`. Also saved to `.tio.tokens.json` by twitchio internally.
- **chat_history.db** — not committed (in .gitignore). Copy manually when moving to new machine.
- **DB connection** — single shared connection via `get_db()`, initialized on first use. Closed via `close_db()` on bot shutdown. All database functions reuse this connection.
- **FTS5 sync** — FTS tables are linked via `content=` and kept in sync by SQLite triggers (AFTER INSERT/DELETE/UPDATE). No manual FTS inserts in application code. FTS search failures are logged (not silently swallowed). On first run with old DB, `_migrate_fts()` auto-detects and rebuilds FTS tables.
- **Graceful shutdown** — SIGTERM handled via `loop.add_signal_handler()`, triggers `close_db()` through `finally` block. SIGKILL cannot be caught (OS-level).
- **Logging** — `logging` module with `basicConfig` in `src/cli.py:main()`. Errors include full tracebacks via `logger.exception()`. `!ask` chunks logged at INFO level.
- **is_caps** — shared via `src/utils.py`, used by `bot.py`.
- **CAPS preserves mentions** — `caps_preserve_mentions()` (in `src/utils.py`) uppercases text but keeps `@mentions` in original case (e.g. `"ТЕКСТ @username ТЕКСТ"`).
- **Cooldown** — expiry time stored **before** Gemini API call and before context fetch (prevents duplicate responses on fast spam). Two tiers: `COOLDOWN_SECONDS` for regular messages (including `!help`, `!stat`), `COOLDOWN_COMMAND_SECONDS` for commands (`!ask`, `!summary`, `!who`, `!versus`). `_cooldowns` dict stores expiry timestamps (not start times).
- **Gemini client** — lazy initialization via `get_client()` in `src/gemini.py`. Created on first Gemini call, not at module import. Allows `--upload-lore` to work without `GEMINI_API_KEY`. `validate_config()` checks `GEMINI_API_KEY` at bot startup but not for CLI commands.
- **Gemini API** — uses native async `client.aio.models.generate_content()`, not `asyncio.to_thread()`.
- **Gemini semaphore** — `asyncio.Semaphore(5)` in `src/gemini.py` limits concurrent Gemini requests. Excess requests queue up instead of hitting rate limits in parallel.
- **Gemini timeout** — `asyncio.wait_for(..., timeout=60)` in `generate()`. Hanging requests return `None` after 60 seconds instead of blocking indefinitely.
- **ContextBuilder** — `src/context.py`. Assembles named sections (`[Label]\ncontent`) into a prompt string. `build()` renders all sections; `build_without('Язык чата', 'Контекст канала')` renders the fallback prompt. Used instead of manual `parts.append(...)` in `_handle_default`, `_handle_who`, `_handle_versus`, `_proactive_loop`.
- **CommandRegistry** — `src/commands.py`. Populated in `ChatComponent.__init__` with 8 entries. `resolve(prompt)` scans entries in registration order — exact match first priority, then prefix. Role check (`vip_mod_broadcaster`) handled by dispatcher in `event_message`, not inside handlers. Adding a new command = one `_registry.add()` line + one `_handle_*` method.
- **CommandContext** — `src/commands.py`. Unified dataclass passed to every `_handle_*` method. Fields: `message`, `user`, `prompt`, `original_text`, `session_id`, `bot`. Replaces the previous per-handler argument lists (was different for each handler).
- **`make_gen_config()`** — shared helper in `src/gemini.py` for Gemini generation config (system instruction, temperature, safety settings, thinking). Used by `_handle_default`, `_proactive_loop`, `!who`, `!versus`. NOT used by `!ask` or `!summary` (which have their own configs).
- **Safety settings** — all 5 harm categories set to `threshold='OFF'` (disables output filter). Input-level filter is server-side and cannot be disabled via API.
- **Fallback retry** — if Gemini returns empty response (likely input filter block), retries with `ctx.build_without('Язык чата', 'Контекст канала')`. Implemented via `ContextBuilder.build_without()`.
- **Thinking** — controlled via `GEMINI_THINKING_BUDGET`. Default `0` (disabled) for faster, cheaper, more impulsive responses. Set to `-1` to let model decide, or a positive number for explicit token budget.
- **Knowledge context** — two sources: FTS5 search (`[Контекст канала]`, keyword-matched from knowledge + chat history) + random sample (`[Язык чата]`, always present). Random sample ensures the bot absorbs chat language even when FTS finds nothing.
- **session_id** — `@property` on `Bot`, recomputed on each access. Auto-transitions at midnight without restart. In `event_message`, computed once as local variable and passed to handlers to avoid race conditions between concurrent coroutines.
- **Follow events** — `ChannelFollowSubscription` via EventSub WebSocket. Requires `moderator:read:followers` scope on bot token. If scope missing, prints re-auth OAuth URL at startup. Bot responds with random hardcoded message from `FOLLOW_MESSAGES` (3 templates, no Gemini call).
- **Proactive messages** — background asyncio task. Controlled via `PROACTIVE_ENABLED` and `PROACTIVE_INTERVAL_MINUTES`. Uses `_send_chat_message()` (HTTP API via `_http.post_chat_message()`). Skips when chat is empty. Reconnect guard in `event_ready` prevents duplicate tasks. Task reference stored in `Bot._proactive_task` (initialized as `None` in `__init__`).
- **`_send_chat_message()`** — sends messages via `_http.post_chat_message()` (twitchio `ManagedHTTPClient`). Used by proactive loop, `!ask` continuation chunks, and `!summary` continuation chunks. Does NOT have reply context (no "Chat Bot" badge on Twitch).
- **`!ask` mode** — factual answers without bot personality. Separate `GenerateContentConfig` with plain-text system instruction, no `prompt.txt`. Markdown stripped from output (`*`, `#`, `_`, bullets, numbered lists). Response split into up to 3 chunks of 450 chars. First chunk sent via `message.respond()` (with bot badge), subsequent via `_send_chat_message()` with 1.5s delay.
- **Response cleanup** — `cleanup_response()` in `src/utils.py`. Leading `@username:` / `@username,` stripped (`.lstrip(':,')`), then remaining `@username` mentions of addressed user removed, truncated to max length. Empty result after cleanup is checked before sending in `!who`, `!versus`, `_handle_default`.
- **`_send_chunked()`** — splits response into up to 3 chunks of 450 chars. Only saves to `bot_interactions` if at least one chunk was successfully sent (partial delivery tracking).

## Environment Variables

| Variable | Notes |
|---|---|
| `TWITCH_CLIENT_ID` | dev.twitch.tv app |
| `TWITCH_CLIENT_SECRET` | dev.twitch.tv app |
| `TWITCH_BOT_ID` | numeric user ID of the bot account |
| `TWITCH_CHANNEL` | streamer's channel name |
| `TWITCH_BOT_TOKEN` | bot OAuth access token (optional on first run) |
| `TWITCH_BOT_REFRESH` | bot OAuth refresh token (optional on first run) |
| `GEMINI_API_KEY` | Google AI Studio |
| `GEMINI_MODEL` | Gemini model name, default `gemini-2.5-flash` |
| `GEMINI_TEMPERATURE` | generation temperature, default `1.5`. Range: 0.0–2.0. Higher = more random |
| `GEMINI_THINKING_BUDGET` | thinking tokens budget. `0` = disabled (default), positive = budget in tokens, `-1` = model decides |
| `CAPS_PROBABILITY` | probability of uppercasing response, default `0.3`. Range: 0.0–1.0 |
| `COOLDOWN_SECONDS` | default `10` |
| `COOLDOWN_COMMAND_SECONDS` | cooldown for `!ask`, `!summary`, `!who`, `!versus`. Default `30` |
| `COOLDOWN_MESSAGE` | supports `{seconds}` placeholder |
| `CONTEXT_CHAT_MESSAGES` | recent chat messages in context (default `50`) |
| `CONTEXT_SEARCH_RESULTS` | FTS results from knowledge + chat history combined (default `10`) |
| `CONTEXT_KNOWLEDGE_RANDOM` | random knowledge entries per request (default `10`) |
| `CONTEXT_WHO_MESSAGES` | user messages for `!who` (default `30`) |
| `CONTEXT_VERSUS_MESSAGES` | user messages per user for `!versus` (default `30`) |
| `PROACTIVE_ENABLED` | enable proactive messages, default `true` |
| `PROACTIVE_INTERVAL_MINUTES` | interval between proactive messages, default `15` |
