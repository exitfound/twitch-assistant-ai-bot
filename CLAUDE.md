# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Twitch chat bot (`securityexpert`) for the `exitfound` channel powered by Gemini 2.5 Flash. Responds to `сосур*` words (сосурян, сосурити, etc.), `@securityexpert` mentions, and replies to bot messages. Stores full chat history in SQLite with FTS5 search and long-term memory.

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

- `bot.py` — `commands.Bot` subclass + `ChatComponent` (handles `event_message`, `event_follow`). Proactive message loop. CLI (`--upload-lore`, `--list-facts`)
- `src/config.py` — config classes: `Twitch`, `Gemini`, `Caps`, `Cooldown`, `Context`, `Proactive`. System prompt loaded from `prompt.txt` via `Gemini.get_system_instruction()`
- `src/database.py` — SQLite async (aiosqlite): shared connection via `get_db()`/`close_db()`, init, save, query, FTS5 search. FTS tables use `content=` with auto-sync triggers. Indexes on `session_id` for `chat_messages` and `bot_interactions`
- `src/knowledge.py` — knowledge base operations: parse lore txt files, import entries, clear knowledge
- `src/utils.py` — shared utilities (`is_caps`)
- `prompt.txt` — system prompt for Gemini (bot personality). Read on every Gemini call — editable without restart

Flow:
1. `setup_hook` — `init_db()` (opens shared DB connection), loads bot token from env, fetches broadcaster ID, registers `ChatComponent`
2. `event_ready` — fetches bot username, calls `_subscribe_to_chat()` (chat + follow events); on failure logs error + prints OAuth URL. Starts `_proactive_loop` if `PROACTIVE_ENABLED`
3. `event_oauth_authorized` — saves token, prints `TWITCH_BOT_TOKEN` / `TWITCH_BOT_REFRESH` to console
4. `ChatComponent.event_message`:
   - Saves every non-bot message to `chat_messages` (FTS synced via trigger)
   - Triggers on: `сосур\w*` regex match (SOSUR_RE) OR `@botname` mention OR reply to bot message
   - Checks per-user cooldown
   - If message == `!help` — replies with list of commands, returns (no Gemini call)
   - If message == `!stat` — queries session + total stats (messages, interactions, session count), returns (no Gemini call)
   - If message == `!top` — queries top-5 users by bot interactions (session + total), returns (no Gemini call)
   - `!fact`/`!defact` — **VIP, moderators, and broadcaster only** (checked via `message.chatter.vip/moderator/broadcaster`)
   - If message starts with `!defact` — finds matching facts by substring (`LIKE '%query%'`). One match → deletes + shows text. Multiple matches → shows list. No match → "такого факта нет". Returns (no Gemini call)
   - If message starts with `!fact` — extracts fact, saves to `facts` (with dedup), replies "запомнил", returns (no Gemini call)
   - If message starts with `!ask` — factual mode: calls Gemini **without** system prompt (`prompt.txt`) and **without** context sections. Own system instruction: concise plain text, no markdown, max 900 chars. Response split into up to 3 messages (450 chars each). First chunk via `message.respond()` (reply with bot badge), subsequent chunks via `_send_chat_message()` with 1.5s delay. Markdown stripped from response. No CAPS mode. Saved with `[ask]` prefix in `bot_interactions`
   - Sets cooldown immediately (before Gemini call, prevents race condition with duplicate requests)
   - Fetches context in parallel: facts + recent chat + context search (FTS across knowledge + chat) + random knowledge
   - Strips leading `@username:` / `@username,` from Gemini response, then removes all remaining `@username` mentions of the addressed user from the body (other users' mentions are kept)
   - Truncates to 450 chars
   - If original message was CAPS (85%+ uppercase letters) OR `random() < CAPS_PROBABILITY` — uppercases the response via `_caps_preserve_mentions()` (preserves `@mentions` in original case)
   - Replies via `message.respond()`
   - Saves interaction to `bot_interactions`
5. `ChatComponent.event_follow`:
   - Triggered on new channel follows (EventSub `channel.follow`)
   - Responds with random message from `FOLLOW_MESSAGES` (3 hardcoded templates, no Gemini call)
   - Saves to `bot_interactions` with `[follow]` as user_message
   - Requires `moderator:read:followers` scope (bot is moderator)
6. `Bot._proactive_loop`:
   - Background asyncio task, started in `event_ready` if `PROACTIVE_ENABLED=true`
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
- **channel:bot not needed** — bot is a moderator in the channel (`/mod securityexpert`).
- **TWITCH_CHANNEL** — streamer's channel (`exitfound`), not the bot's own channel.
- **Bot personality** — `prompt.txt` in project root. Read on every Gemini call via `Gemini.get_system_instruction()` → `_load_prompt()`. Editable without bot restart (hot-reload).
- **Tokens** — `TWITCH_BOT_TOKEN` auto-refreshes via `TWITCH_BOT_REFRESH`. Also saved to `.tio.tokens.json` by twitchio internally.
- **chat_history.db** — not committed (in .gitignore). Copy manually when moving to new machine.
- **DB connection** — single shared connection via `get_db()`, initialized on first use. Closed via `close_db()` on bot shutdown. All database functions reuse this connection.
- **FTS5 sync** — FTS tables are linked via `content=` and kept in sync by SQLite triggers (AFTER INSERT/DELETE/UPDATE). No manual FTS inserts in application code. FTS search failures are logged (not silently swallowed). On first run with old DB, `_migrate_fts()` auto-detects and rebuilds FTS tables.
- **Graceful shutdown** — SIGTERM handled via `loop.add_signal_handler()`, triggers `close_db()` through `finally` block. SIGKILL cannot be caught (OS-level).
- **Logging** — `logging` module with `basicConfig` in `__main__`. Errors include full tracebacks via `logger.exception()`. `!ask` chunks logged at INFO level.
- **is_caps** — shared via `src/utils.py`, used by `bot.py`.
- **CAPS preserves mentions** — `_caps_preserve_mentions()` uppercases text but keeps `@mentions` in original case (e.g. `"ТЕКСТ @username ТЕКСТ"`).
- **Cooldown** — timestamp set **before** Gemini API call (prevents duplicate responses on fast spam).
- **Gemini client** — lazy initialization via `get_genai_client()`. Created on first Gemini call, not at module import. Allows `--upload-lore` to work without `GEMINI_API_KEY`.
- **Gemini API** — uses native async `client.aio.models.generate_content()`, not `asyncio.to_thread()`.
- **`_make_gen_config()`** — shared helper for Gemini generation config (system instruction, temperature, safety settings, thinking). Used by `event_message` and `_proactive_loop`. NOT used by `!ask` (which has its own config without system prompt).
- **Safety settings** — all 5 harm categories set to `threshold='OFF'` (disables output filter). Input-level filter is server-side and cannot be disabled via API.
- **Fallback retry** — if Gemini returns empty response (likely input filter block), retries without `[Язык чата]` and `[Контекст канала]` sections.
- **Thinking** — controlled via `GEMINI_THINKING_BUDGET`. Default `0` (disabled) for faster, cheaper, more impulsive responses. Set to `-1` to let model decide, or a positive number for explicit token budget.
- **Knowledge context** — two sources: FTS5 search (`[Контекст канала]`, keyword-matched from knowledge + chat history) + random sample (`[Язык чата]`, always present). Random sample ensures the bot absorbs chat language even when FTS finds nothing.
- **session_id** — `@property` on `Bot`, recomputed on each access. Auto-transitions at midnight without restart.
- **Follow events** — `ChannelFollowSubscription` via EventSub WebSocket. Requires `moderator:read:followers` scope on bot token. If scope missing, prints re-auth OAuth URL at startup. Bot responds with random hardcoded message from `FOLLOW_MESSAGES` (3 templates, no Gemini call).
- **Proactive messages** — background asyncio task. Controlled via `PROACTIVE_ENABLED` and `PROACTIVE_INTERVAL_MINUTES`. Uses `_send_chat_message()` (HTTP API via `_http.post_chat_message()`). Skips when chat is empty.
- **`_send_chat_message()`** — sends messages via `_http.post_chat_message()` (twitchio `ManagedHTTPClient`). Used by proactive loop and `!ask` continuation chunks. Does NOT have reply context (no "Chat Bot" badge on Twitch).
- **`!ask` mode** — factual answers without bot personality. Separate `GenerateContentConfig` with plain-text system instruction, no `prompt.txt`. Markdown stripped from output (`*`, `#`, `_`, bullets, numbered lists). Response split into up to 3 chunks of 450 chars. First chunk sent via `message.respond()` (with bot badge), subsequent via `_send_chat_message()` with 1.5s delay.
- **Response cleanup** — leading `@username:` and `@username,` stripped from Gemini response (`.lstrip(':,')`), then remaining `@username` mentions of addressed user removed.

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
| `COOLDOWN_MESSAGE` | supports `{seconds}` placeholder |
| `CONTEXT_CHAT_MESSAGES` | recent chat messages in context (default `50`) |
| `CONTEXT_SEARCH_RESULTS` | FTS results from knowledge + chat history combined (default `10`) |
| `CONTEXT_KNOWLEDGE_RANDOM` | random knowledge entries per request (default `10`) |
| `PROACTIVE_ENABLED` | enable proactive messages, default `true` |
| `PROACTIVE_INTERVAL_MINUTES` | interval between proactive messages, default `15` |
