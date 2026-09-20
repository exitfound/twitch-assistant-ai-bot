"""Commands without Gemini: !help-bot, !stat."""
import asyncio

from src.core.commands import CommandContext
from src.core.config import Roll
from src.core.content import Content
from src.core.utils import clean_nick
from src.local import help_announce
from src.core.database import get_session_stats, get_total_stats, get_user_stats


async def handle_help(ctx: CommandContext) -> None:
    # The command list is about to be in chat – the next reminder is not needed
    help_announce.note_help_shown()
    await ctx.message.respond(Content.text('help', user=ctx.user, min=Roll.MIN, max=Roll.MAX))


async def handle_stats(ctx: CommandContext) -> None:
    """!stat – session stats and your own, !stat <nick> – another viewer's stats."""
    target = _target_nick(ctx)
    if target is not None and target != ctx.user:
        await _other_stats(ctx, target)
        return
    (msgs, interactions), (total_msgs, total_interactions, streams, days), own = await asyncio.gather(
        get_session_stats(ctx.session_id),
        get_total_stats(),
        get_user_stats(ctx.session_id, ctx.user),
    )
    # Commands are not written to the DB, so someone who only issues commands in chat
    # has no row of their own – then they get zeros
    own_session_msgs, own_session_inter, own_msgs, own_inter = own or (0, 0, 0, 0)
    # Answer order: personal first, then the stream, then the channel all-time.
    # Each part is its own key, not a placeholder in a shared text: the running bot
    # re-reads CONTENT.md on the fly, and a new placeholder in an old key would go
    # to chat as a raw template until the restart
    live = ctx.bot.stream_live
    # {interactions} – addressings this session, {total_interactions} – all time
    own_text = Content.text(
        'stats_self' if live else 'stats_self_day', user=ctx.user,
        session_msgs=own_session_msgs, interactions=own_session_inter,
        total_msgs=own_msgs, total_interactions=own_inter,
    )
    session_text = Content.text(
        'stats_stream' if live else 'stats_day',
        date=_session_date(ctx.session_id),
        msgs=msgs,
        interactions=interactions,
    )
    total_text = Content.text(
        'stats_total',
        total_msgs=total_msgs,
        total_interactions=total_interactions,
        streams=streams,
        days=days,
    )
    # Blocks are separated by a vertical bar so personal, stream and channel do not merge
    await ctx.message.respond(' | '.join(filter(None, (own_text, session_text, total_text))))


def _session_date(session_id: str) -> str:
    """The session day as 17.09.2026.

    The session id is «2026-09-17 20:12» for a stream and «2026-09-17» outside
    one. Chat needs only the day: the stream start time tells a viewer nothing.
    """
    year, _, rest = session_id[:10].partition('-')
    month, _, day = rest.partition('-')
    if not (year and month and day):
        return session_id
    return f'{day}.{month}.{year}'


async def _other_stats(ctx: CommandContext, target: str) -> None:
    stats = await get_user_stats(ctx.session_id, target)
    if stats is None:
        await ctx.message.respond(Content.text('stats_unknown', user=ctx.user, target=target))
        return
    session_msgs, session_interactions, total_msgs, total_interactions = stats
    await ctx.message.respond(Content.text(
        'stats_user' if ctx.bot.stream_live else 'stats_user_day', user=ctx.user, target=target,
        session_msgs=session_msgs, interactions=session_interactions,
        total_msgs=total_msgs, total_interactions=total_interactions,
    ))


def _target_nick(ctx: CommandContext) -> str | None:
    """Nick from the !stat argument. None – no argument, count the asker's own stats."""
    args = ctx.args.split()
    if not args:
        return None
    return clean_nick(args[0]) or None
