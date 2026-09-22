"""!ascii <ссылка> – draw a picture from chat in braille characters.

The order is: find the link, download it with checks, draw, ask Gemini what
is on it, and only then show it. Gemini does not draw here – drawing must be
deterministic, from pixels – it looks at the picture and either forbids
showing it or describes it briefly. The description does not go to chat: the
bot remembers it and logs it, but does not comment on it out loud.
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
    count_bot_uses_this_stream, record_bot_use, save_bot_interaction,
)
from src.core.viewer import by_tier, tier_of
from src.gemini.client import SAFETY_CHECK, generate, make_gen_config
from src.gemini.output import TWITCH_MSG_MAX
from src.gemini.picture.fetch import BAD_URL, PictureError, fetch
from src.gemini.picture.render import preview, render

logger = logging.getLogger(__name__)

# The link is taken from the original text: ctx.args are cut from the lowercased
# prompt, and a URL path is case-sensitive
URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

# Trailing punctuation: «смотри !ascii http://… .» must not break the address
URL_TRAILING = '.,;:!?)»"\''

# The word Gemini starts its answer with when the picture may be shown. The match is
# on the permission, not the ban: any other form – «извини, но НЕЛЬЗЯ», «это нельзя
# показывать» – counts as a refusal, so an unparsed answer never lets a picture through
ALLOW_WORD = 'МОЖНО'
# The ban word. Only the cache needs it: an answer that starts with neither
# of the two words is not cached – the next attempt may understand better
BLOCK_WORD = 'НЕЛЬЗЯ'
# What is stripped between the permission word and the description
ALLOW_SEPARATORS = ' :.,–-'
# Wrapping the model likes to put around the verdict: **МОЖНО**, «МОЖНО»
VERDICT_WRAPPING = ' *_"\'`«»'

TAG = '[ascii]'

# The kind pictures are recorded under in the bot_uses journal: the per-stream limit
# is counted from it rather than from memory, so a restart does not reset it
USE_KIND = 'ascii'

# A check is not creative work: low temperature so the verdict does not drift
CHECK_TEMPERATURE = 0.2

# Finished art for recent links: one meme runs several times an evening, and each
# repeat would otherwise cost a download, a render and a Gemini request. Lives until
# restart, which covers the repeats, since they happen within a stream
CACHE_SIZE = 32
_cache: OrderedDict[str, tuple[str, str | None]] = OrderedDict()

# Who is currently waiting for their picture. The limit is checked at the start and
# counted after sending, seconds later, and a sub has neither cooldown nor quota, so
# without one-at-a-time several commands pass the same check and overshoot the limit
_busy: set[str] = set()


async def handle_ascii(ctx: CommandContext) -> None:
    if ctx.user in _busy:
        # The first command is still drawing and will answer itself: silently drop
        # the repeat and refund its quota
        await ctx.refuse()
        return
    _busy.add(ctx.user)
    try:
        await _serve(ctx)
    except Exception:
        # Anything past the expected refusals (a Pillow or database error): the viewer
        # paid a quota slot and must hear something rather than nothing
        logger.exception('!ascii: ошибка для %s', ctx.user)
        await ctx.message.respond(Content.text('ascii_failed', user=ctx.user))
    finally:
        _busy.discard(ctx.user)


async def _serve(ctx: CommandContext) -> None:
    limit = _limit_for(ctx.message.chatter)
    if limit and await count_bot_uses_this_stream(ctx.user, USE_KIND, ctx.session_id) >= limit:
        # Limit used up – spend neither traffic nor generation, refund the quota
        await ctx.refuse()
        await ctx.message.respond(Content.text('ascii_no_left', user=ctx.user, limit=limit))
        return

    url = _find_url(ctx)
    if not url:
        # No link – no traffic or generation spent, refund the quota
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
        if verdict is None or _verdict_word(verdict):
            _remember(url, art, verdict)

    description = None
    if verdict is not None:
        description = _allowed(verdict)
        if description is None:
            logger.info('!ascii: %s принёс картинку, которую Gemini показывать не дал: %r',
                        ctx.user, verdict.strip()[:100])
            await ctx.message.respond(Content.text('ascii_blocked', user=ctx.user))
            return

    # The art goes out without a reply: a reply prepends a nick, and the first
    # visual line is already taken by the bot's nick
    if not await ctx.bot.send_chat_message(art):
        # Not sent – the viewer saw no picture, nothing to charge the limit for
        logger.warning('!ascii: картинка для %s не ушла в чат', ctx.user)
        await ctx.message.respond(Content.text('ascii_failed', user=ctx.user))
        return
    # Recorded after sending: a refusal for any reason costs no limit.
    # A cache repeat counts – the viewer still filled the chat with a picture.
    # The picture is already in chat, so an error here is logged, not answered
    try:
        await record_bot_use(ctx.user, USE_KIND)
        if description:
            # Not said in chat, but remembered: otherwise the bot does not know what it
            # showed at all and cannot talk about it later
            logger.info('!ascii: %s принёс %s – %s', ctx.user, url, description)
            await save_bot_interaction(ctx.session_id, ctx.user, f'{TAG} {url}', description)
    except Exception:
        logger.exception('!ascii: картинка для %s показана, но не записана', ctx.user)


def _allowed(verdict: str) -> str | None:
    """The picture's description if Gemini allowed showing it. None – not allowed.

    Permission is only an answer that starts with the word ALLOW_WORD. Everything
    else, including НЕЛЬЗЯ in any wrapping, is a refusal. An empty string means
    allowed, but the model gave no description.
    """
    if _verdict_word(verdict) != ALLOW_WORD:
        return None
    text = verdict.strip().lstrip(VERDICT_WRAPPING)
    return text[len(ALLOW_WORD):].lstrip(ALLOW_SEPARATORS + VERDICT_WRAPPING).strip()


def _verdict_word(verdict: str) -> str | None:
    """ALLOW_WORD or BLOCK_WORD the answer starts with. None – neither."""
    text = verdict.strip().lstrip(VERDICT_WRAPPING).upper()
    for word in (ALLOW_WORD, BLOCK_WORD):
        if text.startswith(word):
            return word
    return None


async def _draw(ctx: CommandContext, url: str) -> tuple[str, str | None] | None:
    """Download, draw and show to Gemini. None – the viewer has already been refused."""
    try:
        data, _ = await fetch(url)
    except PictureError as e:
        # A malformed address costs nothing, but a download costs traffic, so its
        # quota slot is not refunded – otherwise downloads could run forever
        if e.code == BAD_URL:
            await ctx.refuse()
        await ctx.message.respond(Content.text(e.code, user=ctx.user))
        return None

    # Decoding and resizing are CPU-bound: run in a thread so as not to
    # hold up the rest of the chat
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
        # The check did not happen (timeout, network, or Gemini refusing to answer
        # about this picture), so it fails closed – nothing unchecked is shown. Not
        # cached, or one failure would block the link until restart
        logger.warning('!ascii: картинку от %s проверить не удалось, не показываю', ctx.user)
        await ctx.message.respond(Content.text('ascii_unchecked', user=ctx.user))
        return None
    return art, verdict


def _limit_for(chatter) -> int:
    """How many pictures a viewer gets per stream. 0 – unlimited.

    Only those the dispatcher let in get this far: the command is open from the
    subscriber badge up, and is not available at all to a follower or a
    non-follower, so a regular viewer never gets here.
    """
    return by_tier(tier_of(chatter), broadcaster=0, sub=Picture.PER_STREAM_SUB,
                   vip=Picture.PER_STREAM_VIP, regular=Picture.PER_STREAM_VIP)


def _find_url(ctx: CommandContext) -> str | None:
    """The link from the command itself. None – the viewer has to give one.

    There is deliberately no fallback to the last link in chat: guessing picks the
    wrong picture, and commands are not stored anyway.
    """
    found = URL_RE.search(ctx.original_text)
    return found.group(0).rstrip(URL_TRAILING) if found else None


def _remember(url: str, art: str, verdict: str | None) -> None:
    _cache[url] = (art, verdict)
    _cache.move_to_end(url)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


async def _look(data: bytes, ctx: CommandContext) -> str | None:
    """Show the picture to Gemini. Returns its answer as is, or None.

    The only place in the project where Gemini's safety filters are on (SAFETY_CHECK:
    sexual and dangerous content): elsewhere they are off so the persona works, while
    this call checks rather than talks, and the classifier is a second layer beside the
    prompt – it returns nothing for pornography, and an empty answer means the picture is
    not shown. The persona is left out too, since the description goes to memory and the
    log, where flat neutral text is better.
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
    config = make_gen_config(persona=False, temperature=CHECK_TEMPERATURE, safety=SAFETY_CHECK)
    return await generate(contents, config)
