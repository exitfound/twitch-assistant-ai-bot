---
name: refactor-bot
description: Рефакторинг кода Twitch-бота с учётом паттернов проекта (handlers, config, database, Gemini).
disable-model-invocation: true
---

Проведи рефакторинг кода, который обсуждался в текущей беседе. Если конкретная задача не указана — спроси что именно рефакторить.

## Правила работы

1. Сначала прочитай и пойми весь модуль целиком — зависимости, побочные эффекты, контракты
2. Разбей рефакторинг на маленькие шаги, каждый — независимо проверяемый
3. Не меняй поведение — только структуру и читаемость
4. Не добавляй абстракции ради абстракций — три похожие строки лучше преждевременного обобщения
5. Не трогай то, что не относится к задаче
6. После каждого изменения валидируй синтаксис (`python3 -c "import ast; ast.parse(open('file').read())"`)

## Паттерны проекта (соблюдай при рефакторинге)

**bot.py — структура команд:**
- Триггер: константа `*_TRIGGER = '!command'` в начале файла
- Роутинг: `if prompt.startswith(TRIGGER):` в `event_message`, dispatch в `_handle_*`
- Обработчик: `async def _handle_*(self, message, user, ...)` на `ChatComponent`
- Кулдаун: `self.bot._cooldowns[user] = time.time() + Cooldown.*_SECONDS` перед Gemini-вызовом
- Ответ: `message.respond()` (с bot badge) или `self.bot._send_chat_message()` (без badge)

**src/config.py — конфигурация:**
- Каждая группа настроек = отдельный класс (`Twitch`, `Gemini`, `Caps`, `Cooldown`, `Context`, `Proactive`)
- Значения читаются из `os.getenv()` с дефолтами при определении класса
- `validate_config()` — проверка обязательных переменных, вызывается в `run_bot()`

**src/database.py — БД:**
- Одно shared-соединение через `get_db()` + `asyncio.Lock`
- Все функции: `async def`, получают `db = await get_db()`, используют `async with db.execute(...)`
- LIKE-запросы через `_escape_like()`, FTS через `_sanitize_fts_query()`
- FTS-таблицы синхронизируются триггерами — не вставлять в FTS вручную

**src/gemini.py — Gemini:**
- `get_client()` — ленивая инициализация клиента
- `make_gen_config()` — стандартный конфиг с `prompt.txt` + safety + thinking
- `generate()` — async генерация, возвращает `str | None`

**src/utils.py — утилиты:**
- Только чистые функции без side effects
- Константы лимитов Twitch (`TWITCH_MSG_MAX`, `TWITCH_CHUNK_MAX`, и т.д.)

## Приоритеты

- Устранение дублирования (DRY) — только реальное, не кажущееся
- Упрощение условной логики
- Извлечение функций с понятными именами
- Улучшение именования переменных

Для каждого изменения объясни ЗАЧЕМ, а не только ЧТО.
