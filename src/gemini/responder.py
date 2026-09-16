"""Общий конвейер отправки ответа: очистка → CAPS → эмот → отправка → сохранение."""
import asyncio
import logging
import random

from src.core.commands import CommandContext
from src.core.config import Caps, Chat, Emote
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.core.utils import (
    caps_preserve_mentions, cleanup_response, find_banned, is_caps,
    split_into_chunks, strip_markdown,
    CHUNK_SEND_DELAY, TWITCH_CHUNK_MAX, TWITCH_MSG_MAX,
)

logger = logging.getLogger(__name__)


def maybe_add_emote(text: str, max_len: int = TWITCH_MSG_MAX) -> str:
    emotes = Content.items('emotes')
    if emotes and random.random() < Emote.PROBABILITY:
        emote = random.choice(emotes)
        if len(text) + len(emote) + 1 <= max_len:
            return f'{text} {emote}'
    return text


def apply_caps(text: str, source_text: str | None = None) -> str:
    if (source_text is not None and is_caps(source_text)) or random.random() < Caps.PROBABILITY:
        return caps_preserve_mentions(text)
    return text


def passes_moderation(text: str) -> bool:
    """Страховка на случай отключённых safety-фильтров Gemini."""
    banned = find_banned(text, Content.items('banned'))
    if banned:
        logger.warning('Ответ заблокирован стоп-листом (совпадение: %r)', banned)
        return False
    return True


async def respond_and_save(ctx: CommandContext, text: str | None, tag: str,
                           max_len: int = TWITCH_MSG_MAX) -> bool:
    """Отправить ответ в реплай и сохранить. False — если отправлять нечего."""
    if not text:
        return False
    text = cleanup_response(text, ctx.user, max_len)
    if not text or not passes_moderation(text):
        return False
    text = apply_caps(text, ctx.original_text)
    text = maybe_add_emote(text, max_len)
    await ctx.message.respond(f'@{ctx.user}: {text}')
    await save_bot_interaction(ctx.session_id, ctx.user, tag, text)
    return True


async def send_chunked(ctx: CommandContext, text: str | None, tag: str) -> None:
    """Длинный ответ: markdown вырезается, текст режется на чанки."""
    if not text:
        await ctx.message.respond(Content.text('no_answer', user=ctx.user))
        return
    text = strip_markdown(text)
    if not passes_moderation(text):
        await ctx.message.respond(Content.text('filtered', user=ctx.user))
        return

    chunks = split_into_chunks(text, TWITCH_CHUNK_MAX, TWITCH_CHUNK_MAX * Chat.MAX_CHUNKS)
    logger.info('%s: отправка %d чанк(ов), %d символов', tag, len(chunks), len(text))
    sent: list[str] = []
    for i, chunk in enumerate(chunks):
        try:
            if i == 0:
                await ctx.message.respond(f'@{ctx.user}: {chunk}')
            else:
                await asyncio.sleep(CHUNK_SEND_DELAY)
                await ctx.bot.send_chat_message(chunk)
            sent.append(chunk)
        except Exception:
            logger.exception('Не удалось отправить чанк %d/%d для %s', i + 1, len(chunks), tag)
    if sent:
        await save_bot_interaction(ctx.session_id, ctx.user, tag, ' '.join(sent))
