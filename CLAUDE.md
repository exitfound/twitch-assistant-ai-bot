# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Twitch chat bot powered by Gemini 2.5 Flash. Commands (`!who`, `!ask`, …) work as plain chat text. Free text is answered only when it contains a `сосур*` / `secur*` word (сосурян, сосурити, securityexpert, etc.), an `@botname` mention, or is a reply to a bot message. Stores full chat history in SQLite with FTS5 search and long-term memory.

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

# Setup venv from scratch
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Architecture

Entry point is thin; all logic lives in `src/`, split into packages by purpose.

### Where code goes

| Package | Holds | Put here |
|---|---|---|
| `src/core/` | skeleton: config, texts, DB, logging, utils, command registry, chat dispatcher | nothing feature-specific. Features depend on core, never the reverse – the one exception is `component.py`, which registers every feature's commands |
| `src/gemini/` | everything that costs a Gemini call (incl. `picture/` – `!ascii`) | a command that generates text (`kind=KIND_GEMINI`), anything that builds prompts |
| `src/local/` | сосурян's own features without Gemini: simple commands, follow replies, emote spam, the periodic command reminder, and the `roll/` game | a small command or event reply served from SQLite + `CONTENT.md` |
| `src/local/roll/` | the «залупа стрима» game: throws, channel-points rewards, title announcements. A local feature that grew big enough for its own subpackage | anything about `!roll` |
| `src/cli/` | `bot.py --…` commands; the bot does not start | a maintenance command |

A feature that grows its own tables, texts and background loops gets **its own subpackage inside the package of its kind** – `local/<feature>/` like `local/roll/`, or `gemini/<feature>/` if it generates text – instead of spreading across several files of `local/` and `core/`. The owner set this on 2026-09-15: roll is a local feature of the bot, not a top-level one. Table schema and migrations still go into `init_db()` in `src/core/database.py` (one place to keep startup idempotent); the feature's queries live in its package, like `src/local/roll/storage.py`. Each package's `__init__.py` lists its modules. Paths to `CONTENT.md` and `chat_history.db` are resolved with `Path(__file__).parents[2]` – keep that in mind if a module holding them moves.

### Modules

- `bot.py` – `Bot(commands.Bot)` only: lifecycle (`setup_hook`, `event_ready`, `event_oauth_authorized`), EventSub subscriptions, `send_chat_message()`, cooldown storage, background task startup, broadcaster token check (`_add_broadcaster_token()`), reward start (`_start_rewards()`) and pause on `close()`, `run_bot()`. `__main__` delegates to `src/cli/main.py`, falls back to `run_bot()`. Holds the stream state: `stream` (`StreamTracker`), `session_id` / `stream_live`, `fetch_live_stream()`, `_sync_stream()`, `stream_went_online()` / `stream_went_offline()` (shared by the events, startup and the watch loop), `_stream_end_from_vod()`, `event_stream_online` / `event_stream_offline`

**core**
- `src/core/component.py` – `ChatComponent`: command registry wiring, `event_message` dispatcher (`_route()` + cooldown + role check), `event_follow` (delegates to `src/local/follow.py`). Command trigger constants live here; `SOSUR_VARIANTS` / `SOSUR_RE` moved to `src/core/utils.py` on 2026-09-17, because `database.py` needs the same pattern for the one-off backfill of `chat_messages.addressed` and importing the dispatcher there would be circular
- `src/core/commands.py` – `CommandContext` (dataclass: message, user, prompt, original_text, session_id, bot, kind, args; `.clear_cooldown()` releases its own scope), `CommandEntry`, `CommandRegistry`. Entry carries `prefix`, `role` and `kind` (`KIND_LOCAL` / `KIND_GEMINI`). Roles are checked by `_has_role()` in the dispatcher against the **badges**, not the collapsed `_status_of()` status – that one folds subscriber and VIP together for the cooldown, and a subscribing VIP would otherwise be refused where VIPs are let in
- `src/core/config.py` – env only (secrets, numbers, flags): parsing with validation (`_env_int`, `_env_float`, `_env_bool`, `_env_percent`) and config classes `Logging`, `Twitch`, `Gemini`, `Chat`, `Caps`, `Cooldown`, `Stream`, `Roll`, `Rewards`, `Context`, `Proactive`, `Emote`
- `src/core/content.py` – everything the bot says, from `CONTENT.md`: `Content.prompt()`, `.label()`, `.text()`, `.items()`, mtime-cached `_ContentFile`, `validate_content()` (startup check against `REQUIRED`)
- `src/core/database.py` – SQLite async (aiosqlite): `count_bot_uses_since()` counts a journal per stream, not per sliding window; shared connection via `get_db()`/`close_db()`, schema and migrations for **all** tables (roll ones included), chat / facts / knowledge queries (`has_chatted()`, `get_recent_links()` for argument-less `!ascii`), FTS5 search, `backup_db()`, `vacuum_db()`; `streams` queries (`get_stream`, `get_last_stream`, `save_stream`, `end_stream`, `reopen_stream`, `last_chat_time`, `get_session_start`, `get_previous_stream_session`)
- `src/core/followers.py` – `FollowerCache`: фолловер ли зритель, по Helix с TTL-кэшем, ошибка трактуется в пользу зрителя
- `src/core/stream.py` – `StreamTracker`: the session is the stream. `online(stream_id, started_at)` reuses the session of a known stream id (bot restart) or of the previous stream if the new one starts within `STREAM_RESUME_MINUTES` (outage); `offline()` closes it; `settle_missed_end()` closes a stream whose end the bot never saw. Offline the session is the date, as before. A known id that was closed is reopened (`reopen_stream()`). An end the bot never saw comes from the stream's VOD (`Bot._stream_end_from_vod()`: archive whose `created_at` is within 5 min of the start, plus `duration`), else from the last chat message the bot recorded. `watch_stream(bot)` polls Twitch every `CHECK_SECONDS` (120) and applies a disagreement only after `CONFIRMATIONS` (3) consecutive matching checks – catches lost events and changes that happened while the bot was down, without flapping on Helix lag
- `src/core/logging_setup.py` – `setup_logging(default_level)`: console handler always, rotating file handler when `LOG_FILE` is set
- `src/core/utils.py` – shared utilities: `is_caps`, `caps_preserve_mentions`, `strip_markdown`, `split_into_chunks`, `cleanup_response`, `safe_format`, `find_banned`, Twitch message limit constants

**gemini**
- `src/gemini/client.py` – Gemini client (`get_client()`), `generate()` with retries (`contents` is a string or a list of parts – that is how `!ascii` sends the image), `make_gen_config()`, `SAFETY_OFF`
- `src/gemini/context.py` – `ContextBuilder`: assembles Gemini prompts from named sections. `build()` renders full prompt; `build_without(*labels)` renders fallback
- `src/gemini/responder.py` – shared response pipeline: `respond_and_save()` (cleanup → moderation → CAPS → emote → respond as `@ник ответ`, no colon or dash after the nick → save), `send_chunked()`, `maybe_add_emote()`, `apply_caps()`, `passes_moderation()`
- `src/gemini/commands.py` – handlers calling Gemini: `handle_default`, `handle_ask`, `handle_summary`, `handle_who`, `handle_versus`
- `src/gemini/proactive.py` – `proactive_loop()`
- `src/gemini/picture/` – `!ascii`: картинка по ссылке в брайль-арт. `fetch.py` – скачивание со всеми проверками, `render.py` – пиксели в символы Брайля плюс `preview()` для Gemini, `command.py` – хендлер, кэш по ссылке и поиск ссылки в чате

**local**
- `src/local/commands.py` – handlers without Gemini: `handle_help`, `handle_stats`, `handle_fact`, `handle_defact`
- `src/local/follow.py` – `handle_follow()`: random template from `lists.follow`
- `src/local/emote_spam.py` – `emote_spam_loop()`
- `src/local/help_announce.py` – `help_loop()`: раз в `HELP_ANNOUNCE_INTERVAL_MINUTES` шлёт `texts.help_announce` в чат

**local/roll** – the «залупа стрима» game
- `src/local/roll/game.py` – the **only** code that mutates `rolls`: `free_throw()` (the `!roll` limit), `redeem()` (extra / reroll / curse / shield) and `lift_expired_curses()`, all under one `asyncio.Lock`, returning an `Outcome` (status, old/new value, loser, champion). No chat, no Twitch. Also the perks: `grant_perks()`, `appear()`, and inside throws `_take_perk_curse()` / `_perk_shield_left()`
- `src/local/roll/storage.py` – queries on `rolls`, `rewards`, `roll_actions` (`save_roll`, `get_roll`, `set_curse`, `get_session_loser`, `get_session_champion`, reward ids, action journal); `get_last_roll_session_before()` and the `roll_perks` queries
- `src/local/roll/command.py` – `handle_roll`
- `src/local/roll/texts.py` – `reward_title()`, `curse_values()`, `curse_note()`, `champion_note()`: pieces shared by `!roll`, reward outcomes and reward descriptions
- `src/local/roll/redemption.py` – channel-points reward outcomes: `handle_redemption()` applies a redemption via `game.redeem()`, says the result in chat and returns fulfill (`True`) / refund (`False`) / leave alone (`None`, duplicate). No Twitch calls
- `src/local/roll/rewards.py` – Twitch side of rewards: `RewardService` (sync of the four rewards, EventSub subscription with the broadcaster token, redemption status, stale redemptions, pause) and `RewardComponent` (`event_custom_redemption_add`)
- `src/local/roll/announce.py` – `curse_lift_loop()`
- `src/local/roll/perks.py` – chat side of the perks: `on_stream_start()` grants and announces, `on_chat()` starts a player's countdown on their first message (pending names cached per session)

**cli**
- `src/cli/main.py` – argparse and the command bodies (`--upload-lore`, `--list-facts`, `--backup`, `--vacuum`, `--sync-emotes`, …)
- `src/cli/knowledge.py` – knowledge base operations: parse lore txt files, import entries, clear knowledge
- `src/cli/emotes.py` – emote sync: Twitch Helix sources in `SOURCES` (`channel`, `global`), non-destructive `merge()` into the `### emotes` block of `CONTENT.md`

### `CONTENT.md` – everything the bot says

One Markdown file, four sections, read through `src/core/content.py`. Layout is `## section` → `### key` → the value, which runs until the next heading. Re-read automatically when mtime changes – edit without restarting the bot.

| Section | Accessor | Holds |
|---|---|---|
| `## prompts` | `Content.prompt(name, **values)` | `system` (personality), `ask`, `summary`, `summary_request`, `who`, `versus`, `proactive_user`, `proactive_general`, `user_question`, `interaction_line` |
| `## labels` | `Content.label(name, **values)` | context section headings the model sees – `facts`, `chat`, `channel`, `language`, `user_facts`, `user_messages`, `user_interactions`. `prompts.system` refers to them by name, so they must stay in sync |
| `## texts` | `Content.text(name, **values)` | every reply sent to chat – help, stats, roll texts, reward titles/prompts/outcomes/refunds, cooldown, role refusal, usage hints, error messages |
| `## lists` | `Content.items(name)` | `emotes`, `follow`, `banned` – one entry per line, `#` starts a comment. `emotes` is also maintained by `--sync-emotes` |

Rules that make this work:
- **`.env` holds secrets, numbers and flags; `CONTENT.md` holds text.** Nothing the chat sees is hardcoded in handlers
- Placeholders (`{user}`, `{target}`, …) are substituted with `safe_format()` – a broken template logs a warning and passes through instead of crashing the handler
- Markdown has no syntax to break, so the loader checks meaning instead: a duplicate `###` key logs an error (last one wins), and a file that parses to no sections at all keeps the previous valid version
- `<!-- … -->` lines and any prose between `##` and the first `###` are notes – they never reach the bot
- `validate_content()` runs at startup against `REQUIRED` in `src/core/content.py`: a missing key aborts the boot, an unknown heading logs a warning (that is how a typo in `###` surfaces). Add a key there when adding a text

Flow:
1. `setup_hook` – `init_db()` (opens shared DB connection, migrations, legacy cleanup), loads bot token from env, fetches broadcaster ID, adds and validates the broadcaster token (`_add_broadcaster_token()`: token owner must be the channel and carry `channel:manage:redemptions`), registers `ChatComponent` and `RewardComponent`, then syncs the stream: `_sync_stream()` asks Twitch whether the channel is live (`fetch_streams(type="live")`) – events from before the start will never arrive
2. `event_ready` – fetches bot username, calls `_subscribe_to_chat()` (chat + follow events); on failure logs a warning + OAuth URL. Starts background tasks via `_start_background_tasks()` (reconnect guard prevents duplicate tasks), then `_start_rewards()` (no broadcaster token → logs the OAuth URL for the channel account and stays off) Rewards start open only while the stream is live. When live, `perks.on_stream_start()` grants perks for the previous session – idempotent, so reconnects and restarts never duplicate the announcement. Also subscribes to `stream.online` / `stream.offline`
3. `event_oauth_authorized` – saves token; prints `TWITCH_BOT_TOKEN` / `TWITCH_BOT_REFRESH` for the bot account, or `TWITCH_BROADCASTER_TOKEN` / `TWITCH_BROADCASTER_REFRESH` for the channel account and starts rewards right away
4. `ChatComponent.event_message`:
   - Computes `session_id` once as local variable (the session switches when a stream starts or ends, and coroutines outlive that)
   - Saves every non-bot message to `chat_messages` (FTS synced via trigger)
   - `perks.on_chat()` – a player's first message in the stream starts their perk countdown and announces it
   - `_route()` returns `(entry, prompt, addressed)` and runs **before** the message is saved, because `addressed` is stored with it: **every command matches the raw text first**, no addressing needed, and its args are taken verbatim (so `!who securityexpert` keeps the nick). Free text requires `(?:сосур|secur)\w*` (variants listed in `SOSUR_VARIANTS`) / `@botname` / reply to the bot, after which the trigger is stripped and the command lookup runs again (`сосурян !who ник` still works)
   - Resolves the wait from the chatter's status alone (one ladder for everything), then checks it in the scope of the command's class
   - Checks `role='vip_mod_broadcaster'` against `chatter.vip/moderator/broadcaster`
   - Builds `CommandContext` with `args` extracted by the matched entry
   - Sets the cooldown in the class scope declared on the entry, then calls the handler
   - Falls through to `handle_default(ctx)` if no command matched
5. `ChatComponent.event_follow` – random template from `lists.follow`, saved with `[follow]` tag (no Gemini call)
6. Background loops (`src/gemini/proactive.py`, `src/local/emote_spam.py`, `src/local/help_announce.py`, `src/local/roll/announce.py`) – see "Background tasks" below
7. `event_stream_online` / `event_stream_offline` – switch the session (`StreamTracker`), open or pause the rewards, grant the perks of the previous stream. Reruns and premieres (`type != "live"`) are ignored. The same transitions run from `watch_stream()` when Twitch disagrees with the tracker for 3 checks in a row

## Commands

All commands are registered in `ChatComponent.__init__` (10 entries). Adding one = one `add()` line + one handler function.

Commands come in two classes, and the class is also the cooldown scope (`src/core/commands.py`): `KIND_LOCAL` is served from SQLite + `CONTENT.md`, `KIND_GEMINI` costs an API call.

| Command | Class | Role |
|---|---|---|
| `!help-bot` | local | all (the only command a non-follower may use) |
| `!stat` / `!stat <ник>` | local | all |
| `!roll` | local | all |
| `!ascii <ссылка>` | **gemini** | sub/VIP/mod/broadcaster |
| `!summary` | **gemini** | all |
| `!who <ник>` | **gemini** | all |
| `!versus <ник1> <ник2>` | **gemini** | all |
| `!defact <факт>` | local | VIP/mod/broadcaster |
| `!fact <факт>` | local | VIP/mod/broadcaster |
| `!ask <вопрос>` | **gemini** | all |
| _free text addressed to the bot_ | **gemini** | all |

- **Every command works as bare text** (`!who ник`, `!ask …`) – the owner dropped the mention requirement on 2026-09-15, including for Gemini commands. Only the cooldown stands between chat and Gemini spend now. Free text still needs addressing.
- `!roll` – 1–100, lowest roll of the session is the "залупа стрима". **Free throws now depend on status** (owner, 2026-09-18): subscriber or moderator `ROLL_FREE_SUB` (10), VIP `ROLL_FREE_VIP` (5), everyone else `ROLL_FREE_PER_SESSION` (3), broadcaster unlimited; `free_limit_for(chatter)` in `src/local/roll/command.py`. The limit is stored in `rolls.free_limit` on every throw from chat, because a channel-points redemption carries no badges and `_extra()` has to know it. A new throw overwrites the previous value (UPSERT). **`ROLL_FREE_PER_SESSION` (3) free throws per session**, counted in `rolls.free_throws`; beyond that – the `extra` channel-points reward. **The broadcaster rolls without limit** (`free_throw(..., unlimited=True)` from `chatter.broadcaster`): no refusal and no `roll_free_left` note, but the throws are still counted so `extra` behaves for them like for anyone. Free rerolls used to be unlimited; the owner replaced that with the points economy on 2026-09-13 and lowered the limit from 5 to 3 on 2026-09-15. `texts.roll_free_left` is appended as a separate key rather than a placeholder in `roll_loser_*` – see "When changing things". `texts.roll_champion` ("Достойнейший китежанин стрима", the session maximum via `get_session_champion()`, same tie-break as the loser) is appended the same way to `!roll` and to reward outcomes that change a roll; hidden when the champion is also the loser (one player, or everyone rolled the same) **Closed while the stream is offline** (`texts.roll_offline`, cooldown released) – the game lives inside a stream
- `!ascii <ссылка>` – draws a picture from a URL in **braille** (U+2800–U+28FF), **one chat message**. Twitch chat is a proportional font and collapses runs of spaces, so plain ASCII art falls apart; braille cells render at one width, U+2800 is not a space, and one cell carries a 2×4 dot matrix. A braille line holds no spaces, so it is one unbreakable word – spaces sit only between lines, wrapping happens strictly there, and every line lands on its own visual line without newlines (a Twitch message has none). The sender's nick shifts nothing: the first line does not fit beside it and moves down. Hence the budget is **characters, not lines**: a line of N costs N+1, so `_best_box()` picks the frame from the picture's proportions – a tall one gets fewer columns and more rows. `PICTURE_MAX_COLS` (26) caps the width: a wider line breaks on wrap and the picture falls apart. Binarisation is a global threshold first, and **falls back to a local one** when the result comes out flooded (ink share above `INK_MAX`, 0.45) or nearly empty (below `INK_MIN`, 0.05): a single number cannot separate a large area lying just below it, which is how a green Pepe turned into one black silhouette with no face. The local threshold compares a dot to its own neighbourhood (`GaussianBlur` minus the image) and keeps outlines and features inside flat areas. Dithering is not used at all – at 60 dots across Floyd-Steinberg is noise, not halftone. A picture with an **alpha channel** also gets its silhouette outlined (`_add_shape()`): a cutout on transparency carries no form of its own – a gold trophy composited on white is nearly white, the threshold loses it and only a tangle of strokes is left, and Tux's white belly disappears entirely – while the alpha knows the shape exactly. The crop box comes from the alpha too, so it is exact. An image whose alpha turns out fully opaque is treated as having none. After the threshold the ink is **hollowed out** (`PICTURE_HOLLOW`): the drawing minus itself eroded by one dot, so only edges survive. A solid blob loses its shape – a raccoon photo came out as a black smudge with a contour around it – while thin lines are untouched, being all edge already, so drawn line art like Pepe does not change. On top of the contour the darkest places (below `PICTURE_SHADOW`, 45) stay filled: that is what gives volume instead of a wire diagram. `PICTURE_ASPECT` (0.8) squeezes the picture vertically before it is sliced into dots: the chat cell is taller than twice its width, so a braille dot is not square and an uncorrected picture looks stretched. Gemini does **not** draw: it looks at the image and either forbids it (`НЕЛЬЗЯ` → `texts.ascii_blocked`) or describes it (`prompts.picture`). The description never reaches chat – the picture speaks for itself and a remark on top only clogs it. `PICTURE_CHECK` (on) is the whole point of the call: the veto, and the refusal is **closed** – no verdict (timeout, network error, or Gemini refusing to answer about that image) means the picture is not drawn (`texts.ascii_unchecked`), and that failure is not cached, so a retry can still succeed. `_look()` is also **the one place in the project where Gemini safety filters are on**: everywhere else they are off on purpose because the persona needs it, but here the task is the opposite – Google's own classifier becomes a second layer beside the prompt, since an image it will not discuss yields no text, which is already a refusal. The check runs without the persona and at `CHECK_TEMPERATURE` (0.2): a verdict should not drift. **Open from the subscriber badge up** (`ROLE_SUB_VIP_MOD_BROADCASTER`, added 2026-09-18): a follower and a non-follower cannot use it at all. Per stream a VIP draws `PICTURE_PER_STREAM_VIP` (3), a subscriber or moderator `PICTURE_PER_STREAM_SUB` (10), the broadcaster is unlimited (`_limit_for()`). Counted in `bot_uses` under kind `ascii` since the stream start (`count_bot_uses_since()`), so a restart does not reset it; offline, where the session is a date, the window is 24 hours. Only a picture actually shown counts – every refusal is free – while a cache hit does count, because the viewer still filled the chat with a picture. **Without an argument the command takes the last link in chat** (`get_recent_links()`): one viewer drops a picture, another answers `!ascii`, and nobody copies an address. Results are **cached by URL** (`CACHE_SIZE`, 32, in memory, until restart): chat repeats the same meme all evening, and a repeat costs no download, no render and no Gemini call – the verdict is cached too, so a link already approved stays approved for the session. A repeat still spends the hourly quota: the quota limits how often a viewer makes the bot act, not what it costs us. Gemini gets a **downscaled copy** (`preview()`, 512 px, JPEG): the model only has to say what is on the picture and whether it may be shown, and resolution does not change that – on a photo this cuts the payload by about 90%. The description is **saved to `bot_interactions`** under `[ascii] <url>`, so the bot knows what it showed and can talk about it later, but it is never said in chat at the time. `PICTURE_ENABLED=false` **unregisters** the command instead of leaving it silently inert. The SSRF guard checks names *and* the address the connection actually reached (`_check_peer()` via `response.extensions['network_stream']`), because httpx resolves the name a second time and a zero-TTL record would otherwise swap it between check and request. `MAX_PIXELS` is tested between `Image.open()` and `img.load()` – the header carries the size, and a 100 KB png expanding to 150M pixels must be refused before it is decoded, not after. The URL is taken from `ctx.original_text`, not `ctx.args` – the latter is lowercased and a URL path is case-sensitive. `ctx.refuse()` refunds the quota only for a missing or rejected URL: a download that happened cost traffic, and refunding it would make downloading free and unlimited
- `!help-bot` – **lists the command names only**, with no descriptions and no role marks (the `(VIP и модеры)` note on `!fact` / `!defact` was dropped on 2026-09-17). What each one does lives in the channel description under the stream: descriptions stopped fitting into one 450-character Twitch message. The trigger carries the `-bot` suffix so it does not collide with other bots' `!help` (renamed 2026-09-17). `texts.help` still receives `{min}` / `{max}` from the handler, so roll bounds can be put back into the text without touching code
- `!stat` – three keys joined with a space, **the caller's own numbers first**: `texts.stats_self` (or `_day` when offline), then the stream line `texts.stats_stream` (`_day` offline, `Стрим от 17.09.2026` – the day only, no time: the owner cut the clock on 2026-09-17), then `texts.stats_total` for the channel, joined with ` | ` so the three blocks do not run together. **The order inside the answer is the owner's, set 2026-09-17:** own messages this stream → own requests to the bot this stream → own messages and requests all time → the stream's messages and requests → the channel's all-time messages and requests. Channel-wide numbers carry «всего» so they cannot be read as personal ones. `!stat <ник>` answers about another viewer with `texts.stats_user` / `_day`, or `texts.stats_unknown` for a nick never seen in chat. **«Обращений» counts the addressings themselves, not the answers** (the owner's call on 2026-09-17): the number comes from `chat_messages.addressed`, set by the dispatcher for a сосур*/secur* call, an `@botname` mention or a reply. So an addressing counts even when the bot stayed silent (empty Gemini answer, stop-list, cooldown) and when a call carried a command (`сосурити !roll`). Counting `bot_interactions` instead undercounted badly: 3 requests against 7 real addressings in one stream, because that table holds answers, plus everything the bot said on its own. `get_total_stats()` still counts streams and offline days separately (session id longer than 10 chars = stream), but the text no longer shows them – the owner dropped that tail on 2026-09-17; `{streams}` / `{days}` are still passed, so it can be put back by editing `CONTENT.md` alone. `get_user_stats()` returns four numbers, and all four are shown: session messages, session requests, all-time messages, all-time requests
- `!summary` – up to `CONTEXT_SUMMARY_MESSAGES` session messages, `prompts.system` + `prompts.summary` overlay, temperature 1.2, split into chunks
- `!ask` – factual mode: `prompts.ask` as system instruction, no personality and no channel context, markdown stripped
- `!fact` – stores the fact **in its original case** (taken from `original_text`, not the lowercased routing prompt)
- `!defact` – substring match, case-insensitive including Cyrillic (matched in Python, see below)

## Channel-points rewards

Four rewards, created and managed by the bot itself. Twitch lets only the app that created a reward fulfill or refund its redemptions – a reward made by hand in the dashboard is useless to the bot.

| Action | Input | Effect | Refunded when |
|---|---|---|---|
| `extra` | – | a throw for yourself beyond the free ones; does not touch `free_throws` | free throws are still left |
| `reroll` | nick | throws for the target, new value replaces theirs. A target who has not rolled this session gets its first roll this way (`texts.reward_reroll_first`) – allowed since 2026-09-15, but only for a nick that has written in chat at least once, so a typo cannot create a phantom player. On a cursed target the throw is capped by its ceiling and lowers it one step, like the target's own throw. **A successful reroll makes the target immune to further rerolls for `REWARD_REROLL_PROTECT_MINUTES` (3)** – Twitch's per-user limit counts each attacker separately, so several viewers could pile on one target (5 rerolls on one player in 3 minutes on 2026-09-15). Refused rerolls do not extend the window; `curse` pierces it like the shield. Counted by SQLite from the last `ok` reroll in `roll_actions` | bad nick, self, no roll and never seen in chat (`unknown_target`), target shielded, target was rerolled less than `REWARD_REROLL_PROTECT_MINUTES` (3) ago |
| `curse` | nick | **pierces the shield.** Throw capped at `REWARD_CURSE_CEILING` (75); every later throw on the target – its own `!roll` / `extra` **or someone's `reroll`** – lowers the ceiling by `REWARD_CURSE_STEP` (5) down to `REWARD_CURSE_FLOOR` (25). Rerolls used to keep the ceiling; the owner reported that as a bug on 2026-09-15 and it was changed. The ceiling holds on the floor for `REWARD_CURSE_HOLD_MINUTES` (15), then the curse lifts. A new session lifts it too | bad nick, self, target has not rolled, target already cursed |
| `shield` | – | protects from `reroll` until the session ends; not from `curse` | already shielded |

- **Curse state lives in `rolls`:** `curse_ceiling` (ceiling of the next throw, NULL = not cursed) and `curse_floor_at` (`time.time()` when the ceiling reached the floor). Expiry is lazy for the game – `game._curse_of()` in `src/local/roll/game.py` stops treating the row as cursed once the hold has passed. The row itself is cleared by `game.lift_expired_curses()` from `curse_lift_loop`, which announces the lift in chat exactly once (added 2026-09-15 at the owner's request)
- **With 3 free throws most victims never reach the floor on their own:** 75 → 25 takes 10 throws, so without paid `extra` or other viewers' rerolls the curse lasts the whole session at a ceiling of 60 or above. Rerolls cut both ways: each one hurts the victim and brings the floor – and the 15-minute countdown to lifting – closer

- **Fully automatic.** Every redemption is applied and immediately set to FULFILLED or CANCELED (points back). Nobody confirms anything
- **Broadcaster token required.** The bot's moderator rights are not enough. `_add_broadcaster_token()` takes the channel's token from twitchio's store first (loaded from `.tio.tokens.json`, saved there on graceful shutdown and fresher after refreshes), then from `TWITCH_BROADCASTER_TOKEN` / `_REFRESH`. So an OAuth login survives a normal restart even if the console values were never copied – but not a `kill -9`. Without it rewards stay off and the OAuth URL for the channel account is logged; `REWARDS_ENABLED=false` silences that
- **Sync on start, not hot.** Table `rewards` maps action → Twitch reward id. Titles and prompts come from `CONTENT.md`, costs and per-user limits from `.env`; `_changes()` patches only fields that differ. A lost `rewards` table is recovered by matching titles among the app's own rewards
- **Journal `roll_actions`** – one row per redemption, `redemption_id` UNIQUE. It is the duplicate guard (EventSub may redeliver) and the shield store: a shield is an `ok` row with `action='shield'`. Check and write happen under the same lock as the throw
- **Paused while the bot is down.** `Bot.close()` pauses rewards before twitchio closes its HTTP session. After a crash `_settle_stale()` refunds redemptions left UNFULFILLED from before the subscription, or fulfills them if the journal says they were applied Also paused while the stream is offline: `RewardService.set_open()` follows `stream.online` / `stream.offline`, `start(..., open_=)` respects it, and a redemption that slips through offline is refunded with `texts.reward_refund_offline`
- **twitchio 3.x bug:** `CustomRewardRedemption.fulfill()` sends the channel id instead of the redemption id, so every status goes through `_http.patch_custom_reward_redemption()` directly
- **Session = stream (since 2026-09-15).** Free throws, shields, curses and perks belong to the stream: a stream across midnight is one game, and a bot restart or a short outage mid-stream loses nothing. The old calendar-day gap is gone. **Known limit:** with VODs off, if the bot was down long before the stream ended, the end is taken from the last chat message it recorded, so a stream restarted soon after the real end may become a new session

## Perks from the previous stream

At the start of a stream (`stream.online`, or `event_ready` when the bot starts mid-stream) `game.grant_perks()` takes the **previous stream** (`get_previous_stream_session()` from `streams`; if that stream had no rolls there are no perks, older streams do not count; for the first stream after the switch, with no earlier stream recorded, the latest old date session whose rolls ended before this stream started – `get_last_roll_session_before()`) and gives its **champion a shield** and its **loser a curse** (table `roll_perks`, `INSERT OR IGNORE` – safe to repeat). If champion and loser are the same person only the curse is given. Announced once with `texts.roll_perks_both` / `_champion` / `_loser`.

- **Countdown starts on first appearance**, not at stream start: the player's first chat message (`perks.on_chat()` → `game.appear()`, announced with `texts.roll_perk_shield_on` / `roll_perk_curse_on`) or the first throw on them (activated silently inside the game). Lasts `ROLL_PERK_MINUTES` (30)
- **Shield perk** blocks rerolls like a bought shield, refund text `texts.reward_refund_perk_shield` with minutes left (status `perk_shielded`). Curse pierces it
- **Curse perk** is laid on the first throw on the loser inside the window (`_take_perk_curse()` in `_throw_for()`): ceiling `REWARD_CURSE_CEILING`, the usual ladder, **plus a hard deadline** `rolls.curse_until` = end of the window. It is given once (`consumed`); a loser who never shows up inside the window loses it. `curse_lift_loop` announces the lift when the deadline passes
- Chosen by the owner on 2026-09-15: countdown from first appearance, curse = normal curse + 30-minute cap, session = stream for the whole bot, game closed offline

## Background tasks

Each lives in its feature package – `proactive_loop` in `src/gemini/proactive.py`, `emote_spam_loop` in `src/local/emote_spam.py`, `help_loop` in `src/local/help_announce.py`, `curse_lift_loop` in `src/local/roll/announce.py` – all started in `event_ready`, task references on `Bot._proactive_task` / `Bot._emote_spam_task` / `Bot._help_task` / `Bot._curse_lift_task` / `Bot._stream_watch_task` (`watch_stream` in `src/core/stream.py`, always on, sends nothing to chat). The periodic «залупа стрима» announcement (`roll_status_loop`, `ROLL_ANNOUNCE_*`, `texts.roll_announce*`) was removed on 2026-09-15 at the owner's request: every `!roll` and reward outcome already names the loser and the champion.

- `proactive_loop` – after a random pause of `PROACTIVE_INTERVAL_MIN_MINUTES`..`PROACTIVE_INTERVAL_MAX_MINUTES` (5–30), re-rolled before every iteration: a fixed cadence reads as a schedule, the owner asked for the spread on 2026-09-17. Skips empty chat. With `PROACTIVE_TARGET_PROBABILITY` chance targets a random user from the last `PROACTIVE_ACTIVE_WINDOW` messages, otherwise comments generally. Full context, CAPS, emote, moderation check. Saved under `_proactive_` username
- `emote_spam_loop` – after a random pause of `EMOTE_SPAM_INTERVAL_MIN_MINUTES`..`EMOTE_SPAM_INTERVAL_MAX_MINUTES` (5–20), sends `EMOTE_SPAM_MIN`..`EMOTE_SPAM_MAX` random emotes. No Gemini
- `help_announce.help_loop` – every `HELP_ANNOUNCE_INTERVAL_MINUTES` (15) posts `texts.help_announce`, the command list, so a viewer who joined mid-stream learns what the bot can do (added 2026-09-17). **Only while the stream is live and only when somebody has written in the session** – an empty chat gets nothing. Not saved to `bot_interactions`: it is an announcement, not an interaction. No Gemini
- `curse_lift_loop` – runs while `REWARDS_ENABLED=true` or `ROLL_PERKS_ENABLED=true`, checks every `CURSE_LIFT_CHECK_SECONDS` (60, a constant in `announce.py`). `game.lift_expired_curses()` clears, under the game lock, every curse of the current session whose ceiling sat on the floor for `REWARD_CURSE_HOLD_MINUTES` or whose hard deadline `curse_until` passed; each lift is announced with `texts.roll_curse_lifted` and saved as `_roll_` / `[curse-lifted]`. Clear first, send second: a failed send loses that one message but a lift is never announced twice. A curse that never reaches the floor ends silently with the session. No Gemini

The three chat loops send via `Bot.send_chat_message()` (HTTP API, no reply badge), log when stopped, and never die from a single failed iteration.

## Database (chat_history.db)

Ten tables + two FTS5 virtual tables:
- `chat_messages` – all non-bot chat messages with `session_id` and `addressed`
- `bot_uses` – one row per served Gemini request (`username`, `kind`, `created_at`); the hourly quota counts it, `idx_bot_uses_user_time` serves that count
- `bot_interactions` – bot Q&A pairs with `session_id`
- `facts` – persistent facts saved via `!fact`, removable via `!defact`
- `knowledge` – manually imported lore, memes, stream history (unique per content)
- `rolls` – one row per user per session, UPSERT on repeat roll. Holds current state, not throw history. `free_throws` counts free `!roll` throws, `curse_ceiling` / `curse_floor_at` / `curse_until` hold the curse (all added by `_migrate_rolls()` from `_ROLLS_COLUMNS`; old rows get 0 / NULL)
- `rewards` – action → Twitch custom reward id, so renaming a reward does not create a duplicate
- `roll_actions` – journal of redemptions: actor, raw input, target, old/new value, status. Duplicate guard and shield store
- `streams` – Twitch stream id → bot session, `started_at` / `ended_at` (`time.time()`). A known id means a restart, a new id shortly after the previous end means an outage – both keep the session
- `roll_perks` – perks of the previous stream: `(session_id, username, perk)` PK, `from_session`, `active_from` / `active_until` (countdown), `consumed` (curse already laid)
- `chat_fts` / `knowledge_fts` – FTS5 linked via `content=`, synced by triggers

Indexes created by `init_db()`: `idx_knowledge_content`, `idx_chat_messages_session`, `idx_bot_interactions_session`, `idx_chat_messages_username`, `idx_bot_interactions_username`, `idx_facts_username_fact`, `idx_roll_actions_target`.

Session ID = **the stream** while the channel is live (`YYYY-MM-DD HH:MM` of its start, local time), otherwise the current date (`YYYY-MM-DD`), via `Bot.session_id` → `StreamTracker.session_id`. Chat, `!stat`, `!summary`, Gemini context and the game all use it. Old rows keep their date session ids; both formats sort chronologically as strings. A restart mid-stream resumes the same session through the `streams` table

Context sent to Gemini (in order):
1. `[Сохранённые факты]` – asking user's own facts (always) + other users' facts matching prompt words
2. `[Последние сообщения в чате]` – sliding window of recent chat (non-bot messages only, current session)
3. `[Контекст канала]` – FTS5 search across `knowledge` + `chat_messages` (all-time)
4. `[Язык чата]` – random sample from `knowledge` (always present)
5. `{user} спрашивает: {prompt}`

## Key Notes

- **twitchio 3.x** – EventSub (WebSocket), not IRC. Requires `client_id` + `client_secret` from dev.twitch.tv. Dependencies are pinned in `requirements.txt`. Its built-in command system is **switched off**: `Bot.process_commands()` is a no-op, because commands live in our own registry and twitchio would otherwise log `CommandNotFound` with a traceback for every `!…` message and every redemption. Component listeners receive the events independently
- **Bot token bootstrap** – if `TWITCH_BOT_TOKEN` + `TWITCH_BOT_REFRESH` set in `.env`, loaded in `setup_hook` via `add_token()`. First-time users do OAuth once to get values printed to console
- **channel:bot not needed** – bot is a moderator in the channel (`/mod botname`)
- **TWITCH_CHANNEL** – streamer's channel name, not the bot's own channel
- **Logging** – configured by `setup_logging()` in `src/core/logging_setup.py`, called from `run_bot()` (default `INFO`) and from each CLI branch (default `WARNING`). Override with `LOG_LEVEL`; set `LOG_FILE` for a rotating file. Startup, subscriptions, background task state and chunk sends are logged at INFO; errors use `logger.exception()` with tracebacks
- **Hot reload** – `CONTENT.md` is cached by mtime in `src/core/content.py` (`_ContentFile`) and re-read when the file changes. No restart needed. A file with no parsable sections keeps the previous valid version and logs the error
- **DB connection** – single shared connection via `get_db()` (WAL + `synchronous=NORMAL`), closed via `close_db()` on shutdown. All queries serialize on it, so `asyncio.gather` over DB calls gives ordering, not parallelism
- **FTS5 sync** – FTS tables are linked via `content=` and kept in sync by SQLite triggers. `_migrate_fts()` rebuilds old-format tables; `_drop_legacy_objects()` removes the retired `interactions_fts` table and its triggers
- **Fact matching is Python-side** – SQLite `LIKE`/`lower()` only fold ASCII case, so Cyrillic facts are matched with `casefold()` in `_contains_ci()`. Relevant because `!fact` preserves the original case
- **Graceful shutdown** – SIGTERM/SIGINT via `loop.add_signal_handler()`, triggers `close_db()` through `finally`
- **Follow gate** – a viewer who does not follow the channel gets nothing but `texts.follow_required` (and `!help-bot`, so they can see what they are missing). Chosen by the owner on 2026-09-18. Follower state is **not** a chat badge, so it comes from Helix (`fetch_followers(user=…)`, scope `moderator:read:followers`, the one the follow subscription already uses) through `FollowerCache` in `src/core/followers.py`: the answer is `bool(await followers.followers)` – `ChannelFollowers.followers` is an `HTTPAsyncIterator` with no `__bool__`/`__len__`, so testing it without awaiting is always true and the gate silently let everyone through (found in review 2026-09-18); `FOLLOW_CACHE_MINUTES` (15) TTL, invalidated on `event_follow`, and **fails open** – a silent Twitch must not lock the chat out. The hint repeats at most once per `FOLLOW_HINT_MINUTES` (10) per viewer, via a cooldown scope of its own. `FOLLOW_REQUIRED=false` turns the whole gate off
- **Hourly quota** – on top of the cooldown, and **only for `KIND_GEMINI`**: VIP `QUOTA_VIP_PER_HOUR` (60), everyone else `QUOTA_FOLLOWER_PER_HOUR` (30), broadcaster / moderator / subscriber unlimited, `0` = unlimited. The cooldown holds the pace, the quota holds the volume. Counted in SQLite (`bot_uses`, one row per served Gemini request, written **before** the call so a failed generation still counts) so a restart does not reset anyone. A handler that rejects malformed input calls `await ctx.refuse()` – it releases the cooldown *and* deletes the row, so a typo costs nothing
- **Cooldown** – expiry timestamps on `Bot._cooldowns`, keyed `scope:user`. **One ladder by viewer status for every command**: broadcaster / moderator / subscriber wait nothing, VIP waits `COOLDOWN_VIP`, everyone else `COOLDOWN_REGULAR` – resolved by `_cooldown_seconds()`, applied by the dispatcher, never by handlers. The scope is the command's **class**, so the two counters are independent: a wait on `!ask` does not block `!help-bot`. Free text addressed to the bot counts as `KIND_GEMINI` – it calls Gemini like `!ask` does. Status comes from `_status_of()`: broadcaster → moderator → subscriber (founder counts) → vip → regular, checked in that order so a VIP who also subscribes gets the gentler wait. Refusals by **role and by quota** go through `ChatComponent._deny()`, which repeats at most once per `DENY_REPEAT_SECONDS` (30) per viewer in its own `DENY_SCOPE`: those answers never change, and a viewer without badges has no cooldown of their own, so the bot used to reply to every single message. The cooldown message itself is deliberately not throttled – it carries the remaining seconds. Handlers call `ctx.clear_cooldown()` when they reject malformed input – it releases their own scope, so the scope cannot be mismatched. The dict is pruned once it exceeds 500 entries
- **Command matching** – exact match for plain entries; prefix entries require a word boundary, so `!whoever` does not match `!who`, while `!ask:вопрос` still does (separators: space, `:`, `,`)
- **Gemini client** – lazy init via `get_client()`. Semaphore `GEMINI_CONCURRENCY`, timeout `GEMINI_TIMEOUT`, and `GEMINI_RETRIES` retries with exponential backoff on transient errors (5xx, 429, network). **Timeouts are deliberately not retried** – the viewer is already waiting
- **Safety settings** – all 5 harm categories set to `threshold='OFF'`. Input-level filter is server-side and cannot be disabled
- **Output stop-list** – `passes_moderation()` blocks responses containing a word from `lists.banned` before they reach chat. Empty by default; matters because safety filters are off and proactive messages are unattended
- **Fallback retry** – if Gemini returns empty (likely input filter block), `handle_default` retries with `build_without('Язык чата', 'Контекст канала')`
- **Thinking** – `GEMINI_THINKING_BUDGET`, default `0` (disabled). `-1` lets the model decide
- **Random knowledge sampling** – `get_random_knowledge()` picks random ids instead of `ORDER BY RANDOM()`; constant time regardless of table size. `invalidate_knowledge_cache()` must be called after import/clear (already done in `src/cli/knowledge.py`)
- **Probabilities are percentages** – `CAPS_PROBABILITY`, `EMOTE_PROBABILITY`, `PROACTIVE_TARGET_PROBABILITY`, `CONTEXT_SEARCH_KNOWLEDGE_SHARE` are `0..100`. A legacy fractional value like `0.3` is still accepted and logged as deprecated
- **CLI commands run `init_db()`** – including `--backup`. A new migration reaches the live DB the moment any CLI command runs, while the old bot process is still up. Keep migrations additive and harmless to the code that is currently running
- **No tests** – verification is manual, against a copy of the live DB with Gemini stubbed out (and Twitch stubbed for rewards)

## Environment Variables

Required: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_BOT_ID`, `TWITCH_CHANNEL`, `GEMINI_API_KEY`.

`.env.example` is the authoritative list (79 variables, grouped by section) – keep it in sync when adding config. Groups: Twitch credentials (incl. the optional broadcaster token), logging (`LOG_*`), Gemini (`GEMINI_*`), chat output (`CAPS_PROBABILITY`, `EMOTE_PROBABILITY`, `CHAT_MAX_CHUNKS`), cooldowns (`COOLDOWN_VIP` / `COOLDOWN_REGULAR`), Gemini quota (`QUOTA_*`), follow gate (`FOLLOW_*`), context (`CONTEXT_*`), pictures (`PICTURE_*`), command reminder (`HELP_ANNOUNCE_ENABLED`, `HELP_ANNOUNCE_INTERVAL_MINUTES`), proactive (`PROACTIVE_*`), stream (`STREAM_RESUME_MINUTES`), roll (`ROLL_*`, incl. `ROLL_FREE_PER_SESSION`, `ROLL_PERKS_ENABLED`, `ROLL_PERK_MINUTES`), channel-points rewards (`REWARDS_ENABLED`, `REWARD_COST_*`, `REWARD_ATTACK_MAX_PER_USER`, `REWARD_CURSE_*`, `REWARD_REROLL_PROTECT_MINUTES`), emote spam (`EMOTE_SPAM_*`). The two chat loops take a **range**, `*_INTERVAL_MIN_MINUTES` / `*_INTERVAL_MAX_MINUTES`, parsed by `_interval_range()`; the old single `*_INTERVAL_MINUTES` still works and means a fixed interval, with a hint in the log. **No text belongs here** – it goes to `CONTENT.md`.

## When changing things

- **Dash: only `–` (en dash, U+2013).** The em dash (U+2014, deliberately not typed here) is not used anywhere in this project: not in `CONTENT.md`, `.env.example`, the docs, code comments or docstrings, and not in replies to the owner. 750 of them were converted to U+2013 on 2026-09-17 at the owner's request; do not reintroduce them. Check with `grep -rlP '\x{2014}'` over tracked files. `−` (U+2212) stays where it means minus, e.g. `75 − STEP`
- **Docs are part of the change, not a follow-up.** Every change to behaviour, config, schema or layout updates the affected docs in the *same* turn, before reporting the work as done: `CLAUDE.md` (this file), `README.md`, `BOT.md`, `.env.example`, and the `<!-- … -->` notes in `CONTENT.md`. A change that is only documented in the commit message is unfinished. Docs drifted badly twice because updating them was left for later
- Adding a command: one `add()` line in `ChatComponent.__init__` + a handler in the right package (see "Where code goes") + its texts in `## texts` of `CONTENT.md` and in `REQUIRED` (`src/core/content.py`). Then update **all three**: `CLAUDE.md`, `README.md` (command table) and `BOT.md` (registry table). Docs drifted badly once because only `CLAUDE.md` was on the checklist
- Adding config: `src/core/config.py` (with a validated `_env_*` helper) + `.env.example` + README env table. If it is a *text*, it is not config – put it in `CONTENT.md`
- Adding a text: `CONTENT.md` + `REQUIRED` in `src/core/content.py`. Never inline a chat-visible string in a handler
- Editing `CONTENT.md` while the bot runs: the running process hot-reloads it and renders it with **its own, old** code. Adding a placeholder to an existing key before the restart makes `safe_format()` fail and the raw template (`@{user} выбил {value}…`) goes to chat. Add a new key instead
- Changing rewards: title/prompt in `CONTENT.md`, cost/limit in `.env` – both reach Twitch only on restart. Never create the rewards by hand in the dashboard
- Changing the DB schema: `init_db()` must stay idempotent – it runs on every start against a live 90-session database
