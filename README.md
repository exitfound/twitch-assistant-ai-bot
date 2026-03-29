# Twitch AI Bot

Twitch-бот для работы в чате канала на Twitch. Генерирует ответы через **Gemini 2.5 Flash**. Хранит полную историю чата в SQLite с полнотекстовым поиском (FTS5) и долговременной памятью между стримами (опционально).

- Python 3.11+
- [twitchio](https://github.com/TwitchIO/TwitchIO) 3.x — подключение к Twitch через EventSub (WebSocket), не IRC
- [google-genai](https://pypi.org/project/google-genai/) — Gemini API (модель `gemini-2.5-flash`)
- [aiosqlite](https://github.com/omnilib/aiosqlite) — асинхронная работа с SQLite в WAL-режиме
- [python-dotenv](https://pypi.org/project/python-dotenv/) — загрузка переменных окружения из `.env`

---

## Установка

### 1. Создать бот-аккаунт на Twitch

Зарегистрируй отдельный Twitch-аккаунт, который будет выступать ботом (например `securityexpert`). Это обычный аккаунт — специальной регистрации «как бот» не требуется. После создания аккаунта зайди в чат стримерского канала (под стримерским аккаунтом) и дай боту права модератора:

```
/mod botname
```

Модератор нужен чтобы бот мог писать в чат без ограничений Twitch по rate-limit. Делается один раз — разрешение хранится на стороне Twitch и сохраняется при переносе бота.

### 2. Создать приложение на dev.twitch.tv

1. Зайди на [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) под **бот-аккаунтом**
2. **Register Your Application**:
   - **Name** — любое название
   - **OAuth Redirect URLs** — `http://localhost:4343/oauth` (twitchio поднимает локальный сервер на этом порту для OAuth-flow)
   - **Category** — Chat Bot
3. После создания зайди в **Manage** → скопируй **Client ID**
4. Нажми **New Secret** → скопируй **Client Secret** (показывается один раз)

### 3. Узнать числовой ID бот-аккаунта

Twitch API работает с числовыми user ID, а не с никами. Узнать ID бот-аккаунта можно здесь: [streamweasels.com/tools/convert-twitch-username-to-user-id](https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/) — введи ник бота, скопируй числовой ID.

### 4. Получить Gemini API Key

Зайди на [aistudio.google.com](https://aistudio.google.com/) → **Get API key** → создай ключ. Бесплатного тарифа достаточно для минимальной работы бота в чате.

### 5. Установить зависимости

```bash
git clone https://github.com/exitfound/twitch-assistant-ai-bot
cd twitch-assistant-ai-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 6. Настроить .env

```bash
cp .env.example .env
```

Заполни обязательные переменные (подробности в секции [Переменные окружения](#переменные-окружения)):

```env
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_BOT_ID=...
TWITCH_CHANNEL=...
GEMINI_API_KEY=...
```

`TWITCH_CHANNEL` — ник стримера (канал, в котором бот будет работать), **не** ник самого бота.

`TWITCH_BOT_TOKEN` и `TWITCH_BOT_REFRESH` пока оставь пустыми — они будут получены при первом запуске.

### 7. Первый запуск — OAuth-авторизация

```bash
./venv/bin/python3 bot.py
```

При первом запуске бот не сможет подключиться к чату (нет токена) и выведет в консоль OAuth-ссылку:

```
No token found. Open in browser and log in as the bot account:
http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true
```

Открой эту ссылку в браузере. Войти нужно именно **под бот-аккаунтом**, не под стримерским. Разреши запрошенные права. После успешной авторизации twitchio перехватит callback и в консоли появятся токены:

```
Add to .env:
TWITCH_BOT_TOKEN=abc123...
TWITCH_BOT_REFRESH=def456...
```

Скопируй оба значения в `.env`. Останови бота (`Ctrl+C`).

Эта процедура делается **один раз**. В дальнейшем twitchio автоматически обновляет access token через refresh token. Обновлённые токены сохраняются в файл `.tio.tokens.json` (в .gitignore) — это внутренний механизм twitchio.

### 8. Запуск

```bash
./venv/bin/python3 bot.py
```

Успешный запуск выглядит так:

```
Bot started | Username: botname | Session: 2026-03-21

Subscribed to chat #channelname
Subscribed to follow events

Proactive messages enabled (every 15 min)
```

Бот запущен и слушает чат. Для остановки — `Ctrl+C` или `SIGTERM`. При остановке корректно закрывается соединение с БД.

**Примечание:** подписка на follow-события требует scope `moderator:read:followers`. Если при запуске появляется ошибка, переавторизуйте бота по ссылке из консоли.

---

## Использование в чате

### Триггеры

Бот не реагирует на все сообщения в чате — только на три типа:

1. **Слова на `сосур*`** — любое слово, начинающееся с `сосур`: сосурян, сосурити, сосуряночка и т.д. Регулярное выражение: `сосур\w*` (регистронезависимое). Бот найдёт триггер в любом месте сообщения.
2. **`@securityexpert`** — прямое упоминание бота в сообщении (регистронезависимое).
3. **Реплай на сообщение бота** — ответ на любое предыдущее сообщение бота через функцию «Reply» в Twitch-чате.

Все три триггера эквивалентны. После срабатывания триггера бот извлекает текст вопроса: убирает из сообщения упоминание `@securityexpert` и слова на `сосур*`, оставшийся текст отправляет как промпт. Если после очистки текст пустой — отправляет исходное сообщение целиком.

### Команды

Команды работают через те же триггеры. Примеры: `@securityexpert !stat`, `сосурян !help`, реплай на бота с `!fact я из Китежа`.

| Команда | Что делает |
|---|---|
| `!help` | Выводит список доступных команд в чат |
| `!stat` | Показывает статистику текущей сессии (сообщений, обращений) и общую за все сессии |
| `!fact <факт>` | Сохраняет факт о пользователе навсегда. **Только для VIP, модераторов и стримера.** Дублирующие факты игнорируются |
| `!defact <текст>` | Удаляет факт по подстроке. **Только для VIP, модераторов и стримера.** Удалить можно только свои факты |
| `!ask <вопрос>` | Фактический ответ без персонажа бота. Gemini отвечает как обычная модель — без системного промпта, plain text, до 3 сообщений подряд |
| `!summary` | Краткое саммари чата текущей сессии — основные темы, ключевые моменты, активные участники. До 3 сообщений |
| `!who <ник>` | Досье на юзера — бот описывает его на основе фактов и сообщений в своём стиле |
| `!versus <ник1> <ник2>` | Баттл двух юзеров — бот сравнивает по фактам и сообщениям, выбирает победителя |
| Любой другой текст | Отправляется как вопрос в Gemini вместе с контекстом |

### Кулдаун

Каждый пользователь может обращаться к боту не чаще одного раза в N секунд. Два уровня: обычные сообщения (включая `!help`, `!stat`) — `COOLDOWN_SECONDS` (по умолчанию 10), команды (`!ask`, `!summary`, `!who`, `!versus`) — `COOLDOWN_COMMAND_SECONDS` (по умолчанию 30). Кулдаун устанавливается **до** сбора контекста и вызова Gemini API — это предотвращает дублирующие ответы. **Владелец канала (broadcaster) освобождён от кулдауна.** При срабатывании кулдауна бот отвечает сообщением с оставшимся временем ожидания.

### Обработка `!ask`

`!ask` работает иначе: Gemini вызывается **без** системного промпта (`prompt.txt`) и без контекста чата/knowledge. Вместо этого используется отдельная инструкция: «кратко, plain text, без markdown, до 900 символов». Ответ очищается от маркдауна, разбивается на куски до 450 символов и отправляется в 1–3 сообщениях подряд с паузой 1.5 сек. Первое сообщение — reply (со значком «Чат-бот»), остальные — как продолжение.

### Обработка ошибок Gemini

Если Gemini возвращает пустой ответ (вероятная блокировка входным фильтром), бот делает повторный запрос без секций `[Язык чата]` и `[Контекст канала]`. Если повторный запрос тоже пустой — бот отвечает «не удалось получить ответ». Это связано с цензурой в тексте.

---

## Проактивные сообщения

Бот периодически пишет в чат без запроса. Контролируется через `PROACTIVE_ENABLED` и `PROACTIVE_INTERVAL_MINUTES`.

- Первое сообщение — через N минут после старта, затем каждые N минут
- Если чат пустой (нет сообщений в текущей сессии) — пропускает
- С вероятностью 50% обращается к случайному активному пользователю из последних 20 сообщений, 50% — общий комментарий
- Использует полный контекст: `[Последние сообщения в чате]` + `[Язык чата]`
- CAPS с вероятностью `CAPS_PROBABILITY`
- Отправляется через HTTP API (без reply-контекста)

---

## Follow-события

При новом фолловере бот отвечает случайным сообщением из 3 шаблонов (без вызова Gemini):

```
{user} ЗАЛЕТЕЛ НА КАНАЛ. СОСУРИТИ, ФИКСИРУЕМ ПРОНИКНОВЕНИЕ
{user} ЗАФИКСИРОВАН В СИСТЕМЕ. ДОБРО ПОЖАЛОВАТЬ В РОДНУЮ ГАВАНЬ
{user} ТЕПЕРЬ В КИТЕЖ-ГРАДЕ. ОБРАТНОЙ ДОРОГИ НЕТ
```

Требует scope `moderator:read:followers` (бот — модератор канала).

---

## Память и контекст

### Сессии

Session ID — текущая дата в формате `YYYY-MM-DD`. Вычисляется динамически при каждом обращении. Все перезапуски бота в один день — одна и та же сессия. В полночь сессия автоматически меняется без перезапуска.

### Контекст при каждом ответе

При каждом обращении бот параллельно (через `asyncio.gather`) собирает контекст из 4 источников:

| # | Секция | Откуда | Лимит | Когда добавляется |
|---|---|---|---|---|
| 1 | `[Сохранённые факты]` | таблица `facts` (`!fact`) | все свои + релевантные чужие | всегда |
| 2 | `[Последние сообщения в чате]` | `chat_messages` текущей сессии | 50 (env) | если есть сообщения |
| 3 | `[Контекст канала]` | FTS5 по `knowledge` + `chat_messages` | 10 (env) | если FTS нашёл совпадения |
| 4 | `[Язык чата]` | случайная выборка из `knowledge` | 10 (env) | всегда (если knowledge не пуста) |

### System prompt

Хранится в `prompt.txt` в корне проекта. Читается при каждом вызове Gemini (hot-reload — можно редактировать без перезапуска бота). Задаёт характер, стиль, запреты и правила использования контекстных секций.

---

## База данных

SQLite-файл `chat_history.db` в WAL-режиме. Используется одно разделяемое подключение на весь процесс (синглтон через `get_db()`).

При копировании БД копируй все три файла: `chat_history.db`, `chat_history.db-wal`, `chat_history.db-shm`.

### Таблицы

| Таблица | Тип | Описание |
|---|---|---|
| `chat_messages` | обычная | Все сообщения чата (кроме бота). Поля: `session_id`, `username`, `message`, `created_at` |
| `bot_interactions` | обычная | Пары вопрос-ответ. Поля: `session_id`, `username`, `user_message`, `bot_response`, `created_at` |
| `facts` | обычная | Факты через `!fact` / `!defact`. Unique constraint на `username+fact` |
| `knowledge` | обычная | Импортированный лор, мемы, история. Unique index на `content` |
| `chat_fts` | FTS5 | Зеркало `chat_messages`, синхронизируется триггерами |
| `knowledge_fts` | FTS5 | Зеркало `knowledge`, синхронизируется триггерами |

---

## CLI-команды

```bash
./venv/bin/python3 bot.py                                      # запуск бота
./venv/bin/python3 bot.py --upload-lore lore.txt               # импорт лора
./venv/bin/python3 bot.py --upload-lore lore.txt --dry-run     # предпросмотр импорта
./venv/bin/python3 bot.py --upload-lore lore.txt --clear-lore  # очистить + импорт
./venv/bin/python3 bot.py --clear-lore                         # только очистить knowledge
./venv/bin/python3 bot.py --list-facts                         # показать все факты из БД
```

---

## Переменные окружения

### Обязательные

| Переменная | Описание |
|---|---|
| `TWITCH_CLIENT_ID` | Client ID приложения с dev.twitch.tv |
| `TWITCH_CLIENT_SECRET` | Client Secret приложения |
| `TWITCH_BOT_ID` | Числовой user ID бот-аккаунта |
| `TWITCH_CHANNEL` | Ник стримера (канал, в котором работает бот) |
| `GEMINI_API_KEY` | API-ключ Google AI Studio |
| `TWITCH_BOT_TOKEN` | OAuth access token бота (получается при первом запуске) |
| `TWITCH_BOT_REFRESH` | OAuth refresh token бота |

### Опциональные

| Переменная | По умолч. | Описание |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | Название модели Gemini |
| `GEMINI_TEMPERATURE` | `1.5` | Температура генерации (0.0–2.0) |
| `GEMINI_THINKING_BUDGET` | `0` | Бюджет thinking-токенов. `0` = выключен, `-1` = модель решает |
| `CAPS_PROBABILITY` | `0.3` | Вероятность случайного CAPS (0.0–1.0) |
| `COOLDOWN_SECONDS` | `10` | Пауза между запросами одного пользователя |
| `COOLDOWN_COMMAND_SECONDS` | `30` | Пауза после команд `!ask`, `!summary`, `!who`, `!versus` |
| `COOLDOWN_MESSAGE` | `гуляй, отвечу повторно через {seconds} сек.` | Текст при кулдауне |
| `CONTEXT_CHAT_MESSAGES` | `50` | Последних сообщений чата в контексте |
| `CONTEXT_SEARCH_RESULTS` | `10` | FTS результатов из knowledge + chat history |
| `CONTEXT_KNOWLEDGE_RANDOM` | `10` | Случайных записей из knowledge |
| `CONTEXT_WHO_MESSAGES` | `30` | Сообщений юзера для `!who` |
| `CONTEXT_VERSUS_MESSAGES` | `30` | Сообщений на юзера для `!versus` |
| `PROACTIVE_ENABLED` | `true` | Включить проактивные сообщения |
| `PROACTIVE_INTERVAL_MINUTES` | `15` | Интервал проактивных сообщений (минуты) |

---

## Структура проекта

```
├── bot.py                 # Точка входа. Bot (twitchio) + ChatComponent + proactive loop
├── prompt.txt             # System prompt для Gemini (hot-reload без перезапуска)
├── src/
│   ├── cli.py             # CLI: argparse, --upload-lore, --clear-lore, --list-facts
│   ├── commands.py        # CommandRegistry, CommandContext, CommandEntry — роутинг команд
│   ├── config.py          # Настройки: Twitch, Gemini, Caps, Cooldown, Context, Proactive
│   ├── context.py         # ContextBuilder: сборка секционированных промптов для Gemini
│   ├── database.py        # SQLite: таблицы, FTS5 (content= + триггеры), индексы, CRUD, WAL
│   ├── gemini.py          # Gemini-клиент, generate() с семафором и таймаутом, make_gen_config(), SAFETY_OFF
│   ├── knowledge.py       # Работа с базой знаний: парсинг txt, импорт, очистка
│   └── utils.py           # Утилиты: is_caps, caps_preserve_mentions, strip_markdown, cleanup_response
├── requirements.txt       # Зависимости: twitchio, google-genai, python-dotenv, aiosqlite
├── .env                   # Секреты и настройки (не коммитится)
├── .env.example           # Шаблон .env со всеми переменными
└── .gitignore             # Исключает .env, venv/, __pycache__/, chat_history.db и WAL-файлы
```
