---
name: debug-bot
description: Диагностика проблем Twitch-бота — async, twitchio EventSub, Gemini API, SQLite, race conditions.
disable-model-invocation: true
---

Помоги найти и исправить баг в Twitch-боте. Работай в контексте текущей беседы — пользователь опишет симптомы.

## Как диагностировать

### 1. Определи область проблемы

| Симптом | Область | Куда смотреть |
|---------|---------|---------------|
| Бот не запускается | Config / Auth | `validate_config()`, `.env`, `setup_hook()` |
| Бот не отвечает на сообщения | EventSub / Routing | `event_message`, `_subscribe_to_chat()`, триггеры |
| Ответ пустой / "не удалось" | Gemini API | `generate()`, safety filters, `response.text` |
| Ответ обрезан / битый | Post-processing | `cleanup_response()`, `strip_markdown()`, CAPS |
| Команда не работает | Handler | `_handle_*`, кулдаун, права (vip/mod/broadcaster) |
| Данные не сохраняются | Database | `save_*`, `await db.commit()`, FTS triggers |
| FTS поиск пустой | FTS | `_sanitize_fts_query()`, `_migrate_fts()`, `MATCH` |
| Бот дублирует ответы | Race condition | `_cooldowns`, кулдаун ДО вызова Gemini |
| Proactive не работает | Background task | `_proactive_loop`, `PROACTIVE_ENABLED`, `create_task` |
| Follow не работает | EventSub scope | `moderator:read:followers`, `ChannelFollowSubscription` |

### 2. Типичные проблемы

**twitchio 3.x:**
- `event_message` не вызывается → проверь `_subscribe_to_chat()`, нужен валидный токен
- `event_oauth_authorized` не срабатывает → нужно открыть OAuth URL в браузере
- `message.chatter.broadcaster/moderator/vip` — проверь что бот имеет нужные scopes
- Reconnect → `event_ready` вызывается повторно, `_proactive_task` может задвоиться

**Gemini API:**
- `generate()` → `None`: input safety filter (server-side, нельзя отключить) или пустой response
- Fallback retry есть только в `_handle_default` — убирает `[Язык чата]` и `[Контекст канала]`
- `THINKING_BUDGET=0` (default) — thinking отключён, ответы быстрее но менее взвешенные
- `temperature=1.5` — высокая, ответы непредсказуемые. Для фактических вопросов лучше ниже

**SQLite / aiosqlite:**
- `database is locked` → проверь что нет блокирующих операций, WAL mode включён
- FTS пустой после импорта → проверь что триггеры созданы (`_create_fts_triggers`)
- `_migrate_fts()` не сработала → проверь `sqlite_master` на наличие `content=` в DDL

**Async race conditions:**
- `self._session_id` — instance variable на `ChatComponent`, перезаписывается конкурентно
- `_cooldowns` dict — читается/пишется без lock (safe в single-thread asyncio, но edge cases в полночь)
- `get_db()` — защищён `asyncio.Lock`, но лок создаётся при импорте модуля

### 3. Логирование

- `logging.basicConfig(level=logging.WARNING)` — по умолчанию WARNING
- Gemini ошибки: `logger.exception()` в каждом `_handle_*`
- FTS ошибки: `logger.warning()` в `search_context()`
- Для дебага: временно поставь `level=logging.DEBUG` в `__main__`

## Процесс

1. Пользователь описывает симптом
2. Определи область по таблице выше
3. Прочитай релевантный код
4. Предложи гипотезу и способ проверки
5. Если нужно — добавь временный logging для диагностики
6. Исправь баг и объясни root cause
