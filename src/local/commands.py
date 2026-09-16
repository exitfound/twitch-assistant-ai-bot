"""Команды без Gemini: !help, !stat, !fact, !defact."""
import asyncio

from src.core.commands import CommandContext
from src.core.config import Roll
from src.core.content import Content
from src.core.database import delete_fact, get_session_stats, get_total_stats, save_fact

# Сколько фактов и символов показывать, когда !defact нашёл несколько совпадений
DEFACT_PREVIEW_FACTS = 5
DEFACT_PREVIEW_CHARS = 50
DEFACT_DONE_CHARS = 80


async def handle_help(ctx: CommandContext) -> None:
    await ctx.message.respond(Content.text('help', user=ctx.user, min=Roll.MIN, max=Roll.MAX))


async def handle_stats(ctx: CommandContext) -> None:
    (msgs, interactions), (total_msgs, total_interactions, total_sessions) = await asyncio.gather(
        get_session_stats(ctx.session_id),
        get_total_stats(),
    )
    await ctx.message.respond(Content.text(
        'stats',
        user=ctx.user,
        session=ctx.session_id,
        msgs=msgs,
        interactions=interactions,
        total_sessions=total_sessions,
        total_msgs=total_msgs,
        total_interactions=total_interactions,
    ))


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
