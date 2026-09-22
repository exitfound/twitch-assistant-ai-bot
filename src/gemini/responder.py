"""Shared response pipeline: cleanup → CAPS → emote → send → save."""
import asyncio
import logging
import random

from src.core.commands import CommandContext
from src.core.config import Caps, Chat, Emote
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.gemini.output import (
    caps_preserve_mentions, cleanup_response, find_banned, fix_dashes, is_caps,
    split_into_chunks, strip_markdown, trim_to_sentence,
    CHUNK_SEND_DELAY, TWITCH_MSG_MAX,
)

logger = logging.getLogger(__name__)

# How many characters per chunk to keep in reserve for word wrapping
CHUNK_SLACK = 25


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
    """A safety net, since Gemini's safety filters are off."""
    banned = find_banned(text, Content.items('banned'))
    if banned:
        logger.warning('Ответ заблокирован стоп-листом (совпадение: %r)', banned)
        return False
    return True


async def respond_and_save(ctx: CommandContext, text: str | None, tag: str,
                           max_len: int = TWITCH_MSG_MAX, max_chunks: int = 1) -> bool:
    """Send the answer as a reply and save it. False if there is nothing to send.

    max_chunks > 1 lets the answer run over several messages (!versus): the length
    budget is then max_chunks × (450 − CHUNK_SLACK) and max_len is ignored.
    """
    if not text:
        return False
    if max_chunks > 1:
        max_len = max_chunks * (TWITCH_MSG_MAX - CHUNK_SLACK)
    text = cleanup_response(text, ctx.user, max_len)
    if not text or not passes_moderation(text):
        return False
    text = apply_caps(text, ctx.original_text)
    text = maybe_add_emote(text, max_len)
    if max_chunks == 1:
        # The nick followed directly by the text, no colon or dash: that way it reads as a remark
        await ctx.message.respond(f'@{ctx.user} {text}')
        await save_bot_interaction(ctx.session_id, ctx.user, tag, text)
        return True
    chunks = split_into_chunks(text, TWITCH_MSG_MAX, TWITCH_MSG_MAX * max_chunks)[:max_chunks]
    sent = await _send_chunks(ctx, chunks, tag)
    if sent:
        await save_bot_interaction(ctx.session_id, ctx.user, tag, ' '.join(sent))
    # Honestly whether anything reached chat: _send_chunks() swallows send errors, and
    # the caller counts a served request by this, so an unconditional True would charge
    # the viewer's per-stream limit for a failed send
    return bool(sent)


async def _send_chunks(ctx: CommandContext, chunks: list[str], tag: str) -> list[str]:
    """Send chunks: the first as a reply, the rest after a pause. Returns what went out."""
    logger.info('%s: отправка %d чанк(ов), %d символов', tag, len(chunks), sum(map(len, chunks)))
    sent: list[str] = []
    for i, chunk in enumerate(chunks):
        try:
            if i == 0:
                await ctx.message.respond(f'@{ctx.user} {chunk}')
            else:
                await asyncio.sleep(CHUNK_SEND_DELAY)
                await ctx.bot.send_chat_message(chunk)
            sent.append(chunk)
        except Exception:
            logger.exception('Не удалось отправить чанк %d/%d для %s', i + 1, len(chunks), tag)
    return sent


async def send_chunked(
    ctx: CommandContext, text: str | None, tag: str, max_chunks: int | None = None,
) -> bool:
    """A long answer: markdown is stripped, the text is split into chunks.

    max_chunks is the message cap for this command, CHAT_MAX_CHUNKS by default.
    Returns whether anything reached chat.
    """
    if not text:
        await ctx.message.respond(Content.text('no_answer', user=ctx.user))
        return False
    text = fix_dashes(strip_markdown(text))
    if not passes_moderation(text):
        await ctx.message.respond(Content.text('filtered', user=ctx.user))
        return False

    limit = max_chunks or Chat.MAX_CHUNKS
    # Trim up front and at a sentence end, with CHUNK_SLACK in reserve per chunk:
    # chunks are split at words and lose a few characters each, so a text of exactly
    # limit × 450 would spread over limit + 1 chunks – and the last one would be lost
    text = trim_to_sentence(text, limit * (TWITCH_MSG_MAX - CHUNK_SLACK))
    chunks = split_into_chunks(text, TWITCH_MSG_MAX, TWITCH_MSG_MAX * limit)[:limit]
    sent = await _send_chunks(ctx, chunks, tag)
    if sent:
        await save_bot_interaction(ctx.session_id, ctx.user, tag, ' '.join(sent))
    return bool(sent)
