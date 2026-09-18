"""!ascii <ссылка> – нарисовать картинку из чата символами Брайля.

Порядок такой: найти ссылку, скачать с проверками, нарисовать, спросить
Gemini, что там изображено, и только потом показывать. Gemini здесь не
рисует – рисование должно быть детерминированным, из пикселей, – а смотрит
на картинку и либо запрещает её показывать, либо коротко описывает. Описание
в чат не идёт: бот его запоминает и пишет в лог, но вслух не комментирует.
"""
import asyncio
import logging
import re
from collections import OrderedDict

from google.genai import types

from src.core.commands import CommandContext
from src.core.config import Picture
from src.core.content import Content
from src.core.database import (
    count_bot_uses, count_bot_uses_since, get_recent_links, get_session_start,
    record_bot_use, save_bot_interaction,
)
from src.core.utils import TWITCH_MSG_MAX
from src.gemini.client import generate
from src.gemini.picture.fetch import BAD_URL, PictureError, fetch
from src.gemini.picture.render import preview, render

logger = logging.getLogger(__name__)

# Ссылку берём из исходного текста: ctx.args вырезаны из приведённого к
# нижнему регистру prompt, а путь в ссылке регистрозависим
URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

# Хвостовая пунктуация: «смотри !ascii http://… .» не должно ломать адрес
URL_TRAILING = '.,;:!?)»"\''

# Слово, которым Gemini отказывается показывать картинку. Сверяем по началу
# ответа: модель любит дописать объяснение, и это нормально
BLOCK_WORD = 'НЕЛЬЗЯ'

TAG = '[ascii]'

# Под каким видом картинки попадают в журнал bot_uses: лимит за эфир
# считается по нему, а не по памяти, чтобы перезапуск его не обнулял
USE_KIND = 'ascii'

# Вне эфира сессия – это дата, начала стрима нет. Тогда считаем за сутки
OFFLINE_WINDOW_MINUTES = 24 * 60

# Проверка – не творчество: температура низкая, чтобы вердикт не плавал
CHECK_TEMPERATURE = 0.2

# Готовые арты последних ссылок. Чат по природе повторяется: один мем
# прогоняют несколько раз за вечер, и каждый раз это было бы скачивание,
# отрисовка и запрос к Gemini заново. Живёт до перезапуска – этого хватает,
# потому что повторяют в пределах эфира
CACHE_SIZE = 32
_cache: OrderedDict[str, tuple[str, str | None]] = OrderedDict()


async def handle_ascii(ctx: CommandContext) -> None:
    limit = _limit_for(ctx.message.chatter)
    if limit and await _used(ctx) >= limit:
        # Лимит выбран – не тратим ни трафика, ни генерации, квоту возвращаем
        await ctx.refuse()
        await ctx.message.respond(Content.text('ascii_no_left', user=ctx.user, limit=limit))
        return

    url = await _find_url(ctx)
    if not url:
        # Ссылки нет – ни трафика, ни генерации не потрачено, возвращаем квоту
        await ctx.refuse()
        await ctx.message.respond(Content.text('ascii_usage', user=ctx.user))
        return

    cached = _cache.get(url)
    if cached is not None:
        _cache.move_to_end(url)
        art, verdict = cached
    else:
        result = await _draw(ctx, url)
        if result is None:
            return
        art, verdict = result
        _remember(url, art, verdict)

    if verdict and verdict.strip().upper().startswith(BLOCK_WORD):
        logger.info('!ascii: %s принёс картинку, которую Gemini показывать не дал', ctx.user)
        await ctx.message.respond(Content.text('ascii_blocked', user=ctx.user))
        return

    # Арт уходит без реплая: реплай добавляет ник в начало, а первая
    # визуальная строка и так занята ником бота
    if not await ctx.bot.send_chat_message(art):
        # Не ушло – картинки зритель не увидел, лимит за это брать не за что
        return
    # Отмечаем после отправки: отказ по любой причине лимита не стоит.
    # Повтор из кэша считается – зритель всё равно занял чат картинкой
    await record_bot_use(ctx.user, USE_KIND)
    if verdict:
        # В чат не произносим, но запоминаем: иначе бот не знает, что вообще
        # показывал, и не может об этом говорить дальше
        logger.info('!ascii: %s принёс %s – %s', ctx.user, url, verdict.strip())
        await save_bot_interaction(ctx.session_id, ctx.user, f'{TAG} {url}', verdict.strip())


async def _draw(ctx: CommandContext, url: str) -> tuple[str, str | None] | None:
    """Скачать, нарисовать и показать Gemini. None – зрителю уже отказали."""
    try:
        data, _ = await fetch(url)
    except PictureError as e:
        # Кривой адрес не стоил нам ничего, а вот скачивание – стоило трафика,
        # и возвращать за него место в квоте нельзя: иначе качать можно вечно
        if e.code == BAD_URL:
            await ctx.refuse()
        await ctx.message.respond(Content.text(e.code, user=ctx.user))
        return None

    # Декодирование и ресайз упираются в процессор: в поток, чтобы не
    # задерживать остальной чат
    art = await asyncio.to_thread(
        render, data, limit=TWITCH_MSG_MAX, max_cols=Picture.MAX_COLS,
    )
    if not art:
        await ctx.message.respond(Content.text('ascii_failed', user=ctx.user))
        return None

    if not Picture.CHECK:
        return art, None
    verdict = await _look(data, ctx)
    if verdict is None:
        # Проверка не состоялась: таймаут, ошибка сети или сам Gemini
        # отказался отвечать про эту картинку. Отказ должен быть закрытым –
        # непроверенное не показываем. Кэшировать это нельзя, иначе одна
        # неудача заблокировала бы ссылку до перезапуска
        logger.warning('!ascii: картинку от %s проверить не удалось, не показываю', ctx.user)
        await ctx.message.respond(Content.text('ascii_unchecked', user=ctx.user))
        return None
    return art, verdict


def _limit_for(chatter) -> int:
    """Сколько картинок положено зрителю за эфир. 0 – без лимита.

    До сюда доходят только те, кого пустил диспетчер: команда открыта со
    значка подписчика, фолловеру и не фолловеру она недоступна вовсе.
    Поэтому последняя ветка – это VIP.
    """
    if chatter.broadcaster:
        return 0
    if chatter.moderator or chatter.subscriber or chatter.founder:
        return Picture.PER_STREAM_SUB
    return Picture.PER_STREAM_VIP


async def _used(ctx: CommandContext) -> int:
    """Сколько картинок зритель уже нарисовал за этот эфир."""
    start = await get_session_start(ctx.session_id)
    if start is None:
        return await count_bot_uses(ctx.user, USE_KIND, OFFLINE_WINDOW_MINUTES)
    return await count_bot_uses_since(ctx.user, USE_KIND, start)


async def _find_url(ctx: CommandContext) -> str | None:
    """Ссылка из команды, а если её нет – последняя ссылка в чате.

    Обычный ход событий: один зритель кинул картинку, другой ответил
    командой. Заставлять его копировать адрес незачем – весь чат у нас в
    базе.
    """
    found = URL_RE.search(ctx.original_text)
    if found:
        return found.group(0).rstrip(URL_TRAILING)
    for message in await get_recent_links(ctx.session_id):
        found = URL_RE.search(message)
        if found:
            return found.group(0).rstrip(URL_TRAILING)
    return None


def _remember(url: str, art: str, verdict: str | None) -> None:
    _cache[url] = (art, verdict)
    _cache.move_to_end(url)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


async def _look(data: bytes, ctx: CommandContext) -> str | None:
    """Показать картинку Gemini. Вернёт описание, BLOCK_WORD или None.

    Здесь единственное место в проекте, где фильтры безопасности Gemini
    **включены**. В остальном боте они выключены намеренно – персонаж иначе
    не работает, – но тут задача обратная: мы не разговариваем, а проверяем.
    Классификатор Google становится вторым слоем защиты помимо нашего
    промпта: на порнографию он не даст ответить вовсе, запрос вернётся
    пустым, и картинка не будет показана.

    Личность бота тут тоже не нужна: описание идёт в память и в лог, а не в
    чат, и ровный нейтральный текст для этого лучше.
    """
    small = await asyncio.to_thread(preview, data)
    if small is None:
        return None
    image, mime = small
    try:
        contents = [
            types.Part.from_bytes(data=image, mime_type=mime),
            Content.prompt('picture', user=ctx.user),
        ]
    except Exception:
        logger.exception('!ascii: не собрался запрос к Gemini')
        return None
    return await generate(contents, types.GenerateContentConfig(temperature=CHECK_TEMPERATURE))
