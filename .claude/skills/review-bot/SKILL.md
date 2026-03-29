---
name: review-bot
description: Ревью кода Twitch-бота — качество, безопасность, async-паттерны, twitchio/aiosqlite/Gemini специфика.
disable-model-invocation: true
---

Проведи code review изменений в текущем контексте беседы. Если контекста нет — проверь файлы, которые указал пользователь.

## Стек проекта

- Python 3.10+ async
- twitchio 3.x (EventSub WebSocket, не IRC)
- aiosqlite (SQLite + FTS5, одно shared-соединение через `get_db()`)
- Google Generative AI (`google-genai` SDK, async `client.aio.models.generate_content()`)

## Что проверять

**Качество:**
- Читаемость и именование
- Обработка ошибок (без лишней обороны — не оборачивай то, что не падает)
- Соответствие существующим паттернам проекта

**Безопасность:**
- SQL: все запросы через `?` плейсхолдеры, LIKE через `_escape_like()` с `ESCAPE '\\'`
- Утечка токенов/секретов в логи, stdout или ответы в чат
- Валидация пользовательского ввода из чата (Twitch username, command args)
- FTS-запросы через `_sanitize_fts_query()`, не напрямую

**Производительность:**
- Блокирующие вызовы в async-коде (файловый I/O, синхронные HTTP)
- N+1 запросы к БД — проверяй циклы с `await db.execute()`
- Параллельные запросы через `asyncio.gather()` где возможно
- Лимит 450 символов на сообщение Twitch — проверяй truncation

**Async / twitchio:**
- Race conditions: `_cooldowns` dict, `_db` connection, `self._session_id`
- `event_message` вызывается конкурентно — shared state на instance-уровне опасен
- `asyncio.create_task()` — проверяй что таск сохраняется и не дублируется при reconnect
- `event_follow` через EventSub — требует `moderator:read:followers` scope
- Graceful shutdown: `asyncio.Event` + `asyncio.wait()`, `close_db()` в `finally`

**Gemini API:**
- `make_gen_config()` — стандартный конфиг с `prompt.txt` (shared helper)
- `!ask` / `!summary` используют свои отдельные конфиги — не смешивать
- `SAFETY_OFF` — все 5 категорий, проверяй что не потерялись при копировании
- `generate()` может вернуть `None` — всегда обрабатывать
- Fallback retry без `[Язык чата]` и `[Контекст канала]` — только в `_handle_default`

**Паттерны проекта:**
- Каждая `!команда` = константа `*_TRIGGER` + роутинг в `event_message` + метод `_handle_*`
- Кулдаун устанавливается ДО вызова Gemini (предотвращение дублей при спаме)
- Два тира кулдауна: `COOLDOWN_SECONDS` (обычные) и `COOLDOWN_COMMAND_SECONDS` (`!ask`, `!summary`, `!who`, `!versus`)
- `cleanup_response()` для стандартной очистки ответа, `strip_markdown()` для `!ask`/`!summary`
- `caps_preserve_mentions()` — CAPS сохраняет `@mentions` в оригинальном регистре

## Формат ответа

Для каждого замечания:
- Файл и строку
- Серьёзность: **critical** / **warning** / **nit**
- Конкретное предложение по исправлению

В конце — таблица-сводка.
