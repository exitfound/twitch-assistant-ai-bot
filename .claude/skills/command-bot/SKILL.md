---
name: command-bot
description: Добавить новую !команду в Twitch-бота по установленному паттерну проекта.
disable-model-invocation: true
---

Помоги добавить новую команду в Twitch-бота. Следуй установленному паттерну проекта.

## Чеклист добавления команды

Каждая новая команда требует изменений в нескольких местах. Пройди по шагам:

### 1. Триггер (`bot.py`, начало файла)

Добавь константу рядом с существующими:
```python
NEW_TRIGGER = '!commandname'
```

### 2. Обработчик (`bot.py`, класс `ChatComponent`)

Создай метод по шаблону:
```python
async def _handle_newcommand(self, message: twitchio.ChatMessage, user: str, prompt: str) -> None:
    # Кулдаун (если нужен) — ПЕРЕД вызовом Gemini
    self.bot._cooldowns[user] = time.time() + Cooldown.COMMAND_SECONDS
    try:
        # логика команды
        ...
    except Exception:
        logger.exception('!newcommand failed for user %s', user)
        await message.respond(f'@{user}, ошибка.')
```

### 3. Роутинг (`bot.py`, метод `event_message`)

Добавь dispatch в правильном порядке (перед `_handle_default`):
```python
if prompt.startswith(NEW_TRIGGER):
    await self._handle_newcommand(message, user, prompt)
    return
```

Порядок проверок важен: `!defact` перед `!fact`, длинные триггеры перед короткими.

### 4. Help (`bot.py`, метод `_handle_help`)

Добавь описание в строку помощи.

### 5. CLAUDE.md

Обнови секции:
- **Architecture** → описание потока команды в `ChatComponent.event_message`
- **Environment Variables** → если добавляются новые env vars
- **Key Notes** → если есть особенности поведения

## Решения по дизайну (спроси пользователя)

- **Кулдаун**: `COOLDOWN_SECONDS` (10с) или `COOLDOWN_COMMAND_SECONDS` (30с)?
- **Права**: все пользователи, или VIP/mod/broadcaster?
- **Gemini**: нужен вызов LLM? Какой конфиг — `make_gen_config()` (с личностью) или свой?
- **Ответ**: одно сообщение (`message.respond`) или chunked (`_send_chunked`)?
- **Сохранение**: нужно ли сохранять в `bot_interactions`? С каким тегом?
- **CAPS**: применять `caps_preserve_mentions()` или нет?

## Паттерны для ответов

- Простой ответ: `await message.respond(f'@{user}: {text}')`
- Chunked (до 3 частей): `await self._send_chunked(message, user, text, '[tag]')`
- Gemini с личностью: `await generate(prompt, make_gen_config())`
- Gemini без личности: создать свой `GenerateContentConfig` (как в `!ask`)
- Очистка ответа: `cleanup_response(text, user, max_len)`

## Контекст для Gemini (если нужен)

Стандартные блоки (собираются через `asyncio.gather`):
- `[Сохранённые факты]` — `get_relevant_facts(user, prompt)`
- `[Последние сообщения в чате]` — `get_recent_chat(session_id, limit)`
- `[Контекст канала]` — `search_context(prompt, limit)` (FTS)
- `[Язык чата]` — `get_random_knowledge(limit)`

Не все блоки нужны для каждой команды. `!who` использует факты + сообщения + interactions. `!ask` не использует контекст вообще.
