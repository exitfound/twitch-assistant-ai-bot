"""Команды без Gemini: !help-bot, !stat, !fact, !defact."""
import asyncio

from src.core.commands import CommandContext
from src.core.config import Roll
from src.core.content import Content
from src.core.database import (
    delete_fact, get_session_stats, get_total_stats, get_user_stats, save_fact,
)

# Сколько фактов и символов показывать, когда !defact нашёл несколько совпадений
DEFACT_PREVIEW_FACTS = 5
DEFACT_PREVIEW_CHARS = 50
DEFACT_DONE_CHARS = 80


async def handle_help(ctx: CommandContext) -> None:
    await ctx.message.respond(Content.text('help', user=ctx.user, min=Roll.MIN, max=Roll.MAX))


async def handle_stats(ctx: CommandContext) -> None:
    """!stat – статистика сессии и своя, !stat <ник> – статистика другого зрителя."""
    target = _target_nick(ctx)
    if target is not None and target != ctx.user:
        await _other_stats(ctx, target)
        return
    (msgs, interactions), (total_msgs, total_interactions, streams, days), own = await asyncio.gather(
        get_session_stats(ctx.session_id),
        get_total_stats(),
        get_user_stats(ctx.session_id, ctx.user),
    )
    # Своё сообщение с командой уже записано в чат до роутинга, поэтому своя
    # строка есть всегда; нули остаются страховкой на случай сбоя записи
    own_session_msgs, own_session_inter, own_msgs, own_inter = own or (0, 0, 0, 0)
    # Порядок ответа: сначала личное, потом стрим, потом канал за всё время.
    # Каждая часть – свой ключ, а не плейсхолдер в общем тексте: работающий бот
    # перечитывает CONTENT.md на лету, и новый плейсхолдер в старом ключе до
    # перезапуска ушёл бы в чат сырым шаблоном
    live = ctx.bot.stream_live
    # {interactions} – обращения за сессию, {total_interactions} – за всё время
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
    # Блоки разделяются вертикальной чертой: личное, стрим и канал не сливаются
    await ctx.message.respond(' | '.join(filter(None, (own_text, session_text, total_text))))


def _session_date(session_id: str) -> str:
    """День сессии как 17.09.2026.

    Идентификатор сессии – это «2026-09-17 20:12» у эфира и «2026-09-17» вне
    его. В чате нужен только день: время начала стрима зрителю ничего не даёт.
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
    """Ник из аргумента !stat. None – аргумента нет, считаем статистику спрашивающего."""
    args = ctx.args.split()
    if not args:
        return None
    return args[0].lstrip('@').rstrip(',.:;!?').lower() or None


async def handle_fact(ctx: CommandContext) -> None:
    # Регистр берём из исходного текста: ctx.prompt приведён к нижнему для роутинга.
    fact = _original_args(ctx)
    if not fact:
        await ctx.message.respond(Content.text('fact_usage', user=ctx.user))
        return
    await save_fact(ctx.user, fact)
    await ctx.message.respond(Content.text('fact_saved', user=ctx.user))


async def handle_defact(ctx: CommandContext) -> None:
    query = _original_args(ctx)
    if not query:
        await ctx.message.respond(Content.text('defact_usage', user=ctx.user))
        return
    result = await delete_fact(ctx.user, query)
    if result is None:
        await ctx.message.respond(Content.text('defact_missing', user=ctx.user))
    elif isinstance(result, list):
        preview = ' | '.join(
            f[:DEFACT_PREVIEW_CHARS] for f in result[:DEFACT_PREVIEW_FACTS]
        )
        await ctx.message.respond(Content.text(
            'defact_ambiguous', user=ctx.user, count=len(result), preview=preview,
        ))
    else:
        await ctx.message.respond(Content.text(
            'defact_done', user=ctx.user, fact=result[:DEFACT_DONE_CHARS],
        ))


def _original_args(ctx: CommandContext) -> str:
    """Аргументы команды в исходном регистре.

    ctx.args вырезаны из приведённого к нижнему регистру prompt, поэтому для
    фактов ищем ту же подстроку в оригинальном тексте сообщения.
    """
    if not ctx.args:
        return ''
    lowered = ctx.original_text.lower()
    index = lowered.rfind(ctx.args)
    if index == -1:
        return ctx.args
    return ctx.original_text[index:index + len(ctx.args)].strip()
