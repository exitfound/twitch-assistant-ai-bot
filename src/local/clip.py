"""!clip [секунды] [[название]] – the bot clips the last seconds of the stream as its own account.

Twitch creates a clip asynchronously: the request returns an id at once, and the clip
counts as made only once Get Clips lists it. The link goes to chat after that.
"""
import asyncio
import logging
import re

import twitchio
from twitchio.ext import commands

from src.core.commands import CommandContext
from src.core.config import Clip
from src.core.content import Content
from src.core.database import count_all_bot_uses
from src.core.limits import PerStreamLimit
from src.core.tokens import OAUTH_SCOPES_FOLLOWS, oauth_link
from src.core.utils import local_time, reply

logger = logging.getLogger(__name__)

# The kind clips are recorded under in bot_uses: the per-stream limit is counted from it
KIND = 'clip'

# Twitch's own ceiling on a clip's length
MAX_SECONDS = 60

# A title longer than this is cut: Twitch refuses the whole clip over a long title
TITLE_MAX = 100

# The length is bare digits: «30с», «7.5» and «-5» are refused, not guessed at
_LENGTH_RE = re.compile(r'\d{1,3}')
# A single word before the brackets that starts like a number is a malformed length;
# several words, or one that is not number-like, are a title without brackets
_LENGTH_START_RE = re.compile(r'[+-]?\d')

# The generated title's date, in BOT_TIMEZONE
TITLE_DATE_FORMAT = '%d.%m.%Y %H:%M'

# Twitch lists a finished clip within seconds; one not listed after a minute has failed
CONFIRM_SECONDS = 60
POLL_SECONDS = 3

# How much of a malformed length is quoted back in the refusal
INPUT_PREVIEW_CHARS = 25


class BadArgs(Exception):
    """Malformed !clip args; key is the refusal text, value – what to quote back."""

    def __init__(self, key: str, value: str = '') -> None:
        super().__init__(key)
        self.key = key
        self.value = value[:INPUT_PREVIEW_CHARS]


def parse(args: str) -> tuple[int, str]:
    """(seconds, title) from «[секунды] [[название]]». An empty title – the bot names the clip.

    The title is everything between the first «[» and a closing «]» at the very end, so
    spaces, punctuation and inner brackets pass as written. Raises BadArgs.
    """
    rest, title = args.strip(), ''
    if '[' in rest or ']' in rest:
        start = rest.find('[')
        if start == -1 or not rest.endswith(']') or len(rest) == start + 1:
            raise BadArgs('clip_usage')
        rest, title = rest[:start].strip(), rest[start + 1:-1].strip()
    if not rest:
        return Clip.DEFAULT_SECONDS, title[:TITLE_MAX]
    if len(rest.split()) > 1 or not _LENGTH_START_RE.match(rest):
        # «!clip 30 момент»: the length is fine, the title lacks its brackets
        raise BadArgs('clip_usage')
    if not _LENGTH_RE.fullmatch(rest) or not Clip.MIN_SECONDS <= int(rest) <= MAX_SECONDS:
        raise BadArgs('clip_bad_length', rest)
    return int(rest), title[:TITLE_MAX]


async def default_title(user: str) -> str:
    """«Клип #N …»: N counts every clip the bot has made. Two clips made in the same
    moment may share a number; the date and the nick still tell them apart."""
    number = await count_all_bot_uses(KIND) + 1
    date = local_time().strftime(TITLE_DATE_FORMAT)
    return Content.text('clip_title', number=number, user=user, date=date)[:TITLE_MAX]


def _limit_for(chatter) -> int:
    """Clips per stream. Only badge holders get this far; the broadcaster is unlimited."""
    return 0 if chatter.broadcaster else Clip.PER_STREAM


LIMIT = PerStreamLimit(KIND, _limit_for, 'clip_failed')


async def handle_clip(ctx: CommandContext) -> None:
    if not ctx.bot.stream_live:
        # Twitch clips only a live stream
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('clip_offline', user=ctx.user))
        return
    try:
        seconds, title = parse(ctx.original_args)
    except BadArgs as e:
        await ctx.refuse()
        await reply(ctx.message, Content.text(
            e.key, user=ctx.user, input=e.value,
            min=Clip.MIN_SECONDS, max=MAX_SECONDS, default=Clip.DEFAULT_SECONDS,
        ))
        return
    await LIMIT.run(ctx, lambda: _serve(ctx, seconds, title))


async def _serve(ctx: CommandContext, seconds: int, title: str) -> bool:
    """Make the clip and post its link. True – the clip exists and counts toward the limit."""
    title = title or await default_title(ctx.user)
    try:
        url = await ctx.bot.create_clip(seconds, title)
    except twitchio.HTTPException as e:
        key = _refusal_key(e)
        if key == 'clip_bad_title':
            # The title failed AutoMod: nothing was made, the viewer may try again
            await ctx.refuse()
        await reply(ctx.message, Content.text(key, user=ctx.user))
        return False
    if url is None:
        logger.warning('!clip: клип для %s не появился за %d с', ctx.user, CONFIRM_SECONDS)
        await reply(ctx.message, Content.text('clip_failed', user=ctx.user))
        return False
    logger.info('!clip: %s – %d с, %s', ctx.user, seconds, url)
    await reply(ctx.message, Content.text('clip_done', user=ctx.user, url=url, seconds=seconds))
    return True


def _refusal_key(error: twitchio.HTTPException) -> str:
    """The text for a Twitch refusal; the ones the owner has to fix are logged."""
    detail = str(error.extra.get('message', ''))
    if error.status == 404:
        # Twitch no longer sees the stream: it ended before the bot noticed
        return 'clip_offline'
    if error.status == 400 and 'automod' in detail.lower():
        return 'clip_bad_title'
    if error.status in (400, 403):
        # A category that cannot be clipped, clips switched off or restricted to followers
        # and subscribers, or the bot banned in the channel
        logger.warning('!clip: Twitch отказал (%d): %s', error.status, detail)
        return 'clip_rejected'
    if error.status == 401:
        logger.warning('!clip: у токена бота нет права clips:edit. Открой в браузере и войди '
                       'под аккаунтом бота:\n%s', oauth_link(OAUTH_SCOPES_FOLLOWS))
        return 'clip_failed'
    logger.warning('!clip: ошибка Twitch (%d): %s', error.status, detail)
    return 'clip_failed'


async def make_clip(client: commands.Bot, channel_id: str, bot_id: str,
                    seconds: int, title: str | None) -> str | None:
    """Clip the channel's stream as the bot and wait until Twitch lists it.

    Returns the clip's link, or None if it never appeared. A refusal raises HTTPException.
    """
    created = await client.create_partialuser(channel_id).create_clip(
        token_for=bot_id, duration=seconds, title=title,
    )
    for _ in range(CONFIRM_SECONDS // POLL_SECONDS):
        await asyncio.sleep(POLL_SECONDS)
        try:
            clips = await client.fetch_clips(clip_ids=[created.id])
        except Exception:
            # One failed check is not a failed clip: the next one may see it
            logger.warning('!clip: не удалось проверить клип %s', created.id, exc_info=True)
            continue
        if clips:
            return clips[0].url
    return None
