"""Команды, обращающиеся к Gemini."""
import asyncio
import logging

from google.genai import types

from src.core.commands import CommandContext
from src.core.config import Context, Gemini
from src.core.content import Content
from src.core.database import (
    get_random_knowledge, get_recent_chat, get_relevant_facts,
    get_user_interactions, get_user_messages, search_context,
)
from src.core.utils import WHO_VERSUS_MAX
from src.gemini.client import SAFETY_OFF, generate, make_gen_config
from src.gemini.context import ContextBuilder
from src.gemini.responder import respond_and_save, send_chunked

logger = logging.getLogger(__name__)

SUMMARY_TEMPERATURE = 1.2


def _interaction_lines(username: str, interactions: list[tuple[str, str]]) -> list[str]:
    return [
        Content.prompt('interaction_line', user=username, question=q, answer=a)
        for q, a in interactions
    ]


async def handle_default(ctx: CommandContext) -> None:
    facts, recent_chat, context_results, random_knowledge = await asyncio.gather(
        get_relevant_facts(ctx.user, ctx.prompt),
        get_recent_chat(ctx.session_id, Context.CHAT_MESSAGES),
        search_context(ctx.prompt, Context.SEARCH_RESULTS),
        get_random_knowledge(Context.KNOWLEDGE_RANDOM),
    )
    prompt_ctx = (
        ContextBuilder()
        .add_facts(Content.label('facts'), facts)
        .add_chat(Content.label('chat'), recent_chat)
        .add_lines(Content.label('channel'), context_results)
        .add_lines(Content.label('language'), random_knowledge)
        .add_raw(Content.prompt('user_question', user=ctx.user, prompt=ctx.prompt))
    )
    try:
        gen_config = make_gen_config()
        text = await generate(prompt_ctx.build(), gen_config)
        if not text:
            logger.warning('Пустой ответ для %s, повтор без контекста канала', ctx.user)
            text = await generate(
                prompt_ctx.build_without(Content.label('language'), Content.label('channel')),
                gen_config,
            )
        if not await respond_and_save(ctx, text, ctx.prompt):
            logger.warning('Ответ не отправлен для %s, запрос: %s', ctx.user, ctx.prompt[:100])
            await ctx.message.respond(Content.text('no_answer', user=ctx.user))
    except Exception:
        logger.exception('Gemini: ошибка генерации для %s', ctx.user)
        await ctx.message.respond(Content.text('gen_error', user=ctx.user))


async def handle_ask(ctx: CommandContext) -> None:
    if not ctx.args:
        ctx.clear_cooldown()
        await ctx.message.respond(Content.text('ask_usage', user=ctx.user))
        return
    try:
        ask_config = types.GenerateContentConfig(
            system_instruction=Content.prompt('ask'),
            temperature=Gemini.TEMPERATURE,
            safety_settings=SAFETY_OFF,
        )
        text = await generate(ctx.args, ask_config)
        await send_chunked(ctx, text, f'[ask] {ctx.args}')
    except Exception:
        logger.exception('Gemini !ask: ошибка для %s', ctx.user)
        await ctx.message.respond(Content.text('ask_error', user=ctx.user))


async def handle_summary(ctx: CommandContext) -> None:
    try:
        recent_chat = await get_recent_chat(ctx.session_id, Context.SUMMARY_MESSAGES)
        if not recent_chat:
            ctx.clear_cooldown()
            await ctx.message.respond(Content.text('summary_empty', user=ctx.user))
            return
        chat_lines = '\n'.join(f'{u}: {m}' for u, m in recent_chat)
        summary_config = types.GenerateContentConfig(
            system_instruction=f"{Content.prompt('system')}\n\n{Content.prompt('summary')}",
            temperature=SUMMARY_TEMPERATURE,
            safety_settings=SAFETY_OFF,
        )
        request = Content.prompt(
            'summary_request', session_id=ctx.session_id, count=len(recent_chat),
        )
        text = await generate(f'{request}\n\n{chat_lines}', summary_config)
        await send_chunked(ctx, text, '[summary]')
    except Exception:
        logger.exception('Gemini !summary: ошибка для %s', ctx.user)
        await ctx.message.respond(Content.text('summary_error', user=ctx.user))


async def handle_who(ctx: CommandContext) -> None:
    args = ctx.args.split()
    target = args[0].lstrip('@') if args else ''
    if not target:
        ctx.clear_cooldown()
        await ctx.message.respond(Content.text('who_usage', user=ctx.user))
        return
    try:
        target_facts, target_msgs, target_interactions = await asyncio.gather(
            get_relevant_facts(target, ''),
            get_user_messages(target, Context.WHO_MESSAGES),
            get_user_interactions(target, Context.USER_INTERACTIONS),
        )
        if not target_facts and not target_msgs and not target_interactions:
            await ctx.message.respond(Content.text('who_unknown', user=ctx.user, target=target))
            return
        prompt_ctx = (
            ContextBuilder()
            .add_facts(Content.label('user_facts', target=target), target_facts)
            .add_lines(Content.label('user_messages', target=target), target_msgs)
            .add_lines(
                Content.label('user_interactions', target=target),
                _interaction_lines(target, target_interactions),
            )
            .add_raw(Content.prompt('who', user=ctx.user, target=target))
        )
        text = await generate(prompt_ctx.build(), make_gen_config())
        if not await respond_and_save(ctx, text, f'[who] {target}', WHO_VERSUS_MAX):
            await ctx.message.respond(Content.text('who_failed', user=ctx.user, target=target))
    except Exception:
        logger.exception('Gemini !who: ошибка для %s', ctx.user)
        await ctx.message.respond(Content.text('gen_failed', user=ctx.user))


async def handle_versus(ctx: CommandContext) -> None:
    args = ctx.args.split()
    nicks = list(dict.fromkeys(a.lstrip('@') for a in args if a.lstrip('@')))
    if len(nicks) < 2:
        ctx.clear_cooldown()
        await ctx.message.respond(Content.text('versus_usage', user=ctx.user))
        return
    nick1, nick2 = nicks[0], nicks[1]
    try:
        facts1, msgs1, interactions1, facts2, msgs2, interactions2 = await asyncio.gather(
            get_relevant_facts(nick1, ''),
            get_user_messages(nick1, Context.VERSUS_MESSAGES),
            get_user_interactions(nick1, Context.USER_INTERACTIONS),
            get_relevant_facts(nick2, ''),
            get_user_messages(nick2, Context.VERSUS_MESSAGES),
            get_user_interactions(nick2, Context.USER_INTERACTIONS),
        )
        if not any([facts1, msgs1, interactions1, facts2, msgs2, interactions2]):
            await ctx.message.respond(
                Content.text('versus_unknown', user=ctx.user, nick1=nick1, nick2=nick2)
            )
            return
        prompt_ctx = ContextBuilder()
        for nick, facts, msgs, ints in [(nick1, facts1, msgs1, interactions1),
                                        (nick2, facts2, msgs2, interactions2)]:
            prompt_ctx.add_facts(Content.label('user_facts', target=nick), facts)
            prompt_ctx.add_lines(Content.label('user_messages', target=nick), msgs)
            prompt_ctx.add_lines(
                Content.label('user_interactions', target=nick), _interaction_lines(nick, ints),
            )
        prompt_ctx.add_raw(Content.prompt('versus', user=ctx.user, nick1=nick1, nick2=nick2))
        text = await generate(prompt_ctx.build(), make_gen_config())
        if not await respond_and_save(ctx, text, f'[versus] {nick1} vs {nick2}', WHO_VERSUS_MAX):
            await ctx.message.respond(Content.text('versus_failed', user=ctx.user))
    except Exception:
        logger.exception('Gemini !versus: ошибка для %s', ctx.user)
        await ctx.message.respond(Content.text('gen_failed', user=ctx.user))
