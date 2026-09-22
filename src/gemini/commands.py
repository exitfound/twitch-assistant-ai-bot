"""Commands that call Gemini."""
import logging
from collections import defaultdict


from src.core.commands import CommandContext
from src.core.config import Gemini, Summary, Who
from src.core.content import Content
from src.core.database import (
    count_bot_uses_this_stream, get_last_tagged_interaction, record_bot_use,
)
from src.core.utils import (
    WHO_MAX, clean_nick, reply_to_bot,
)
from src.gemini import summary, who
from src.gemini.answer_context import Question, answer, walk
from src.gemini.client import generate, make_gen_config
from src.gemini.responder import respond_and_save, send_chunked

logger = logging.getLogger(__name__)

# !versus: two descriptions plus the verdict at the end do not fit one message, and
# trimming a single message cuts exactly the verdict
VERSUS_MAX_CHUNKS = 2

ASK_TAG = '[ask]'
# An answer to !ask is at most two Twitch messages. send_chunked trims the excess
# at a sentence end
ASK_MAX_CHUNKS = 2
# For how many minutes a viewer's previous !ask counts as the same conversation
ASK_FOLLOWUP_MINUTES = 15


async def handle_default(ctx: CommandContext) -> None:
    """Free text addressed to the bot. The context and its fallback ladder live in
    src/gemini/answer_context.py: two streams plus memory, less when Gemini blocks it."""
    question = Question(
        session_id=ctx.session_id, user=ctx.user, prompt=ctx.prompt,
        replied=reply_to_bot(ctx.message, ctx.bot.bot_id),
    )
    try:
        text, _ = await answer(question, make_gen_config())
        if not await respond_and_save(ctx, text, ctx.prompt):
            logger.warning('Ответ не отправлен для %s, запрос: %s', ctx.user, ctx.prompt[:100])
            await ctx.message.respond(Content.text('no_answer', user=ctx.user))
    except Exception:
        logger.exception('Gemini: ошибка генерации для %s', ctx.user)
        await ctx.message.respond(Content.text('gen_error', user=ctx.user))


async def handle_ask(ctx: CommandContext) -> None:
    # The question in its original case: «РФ», names and code lose their meaning without it
    question = ctx.original_args
    if not question:
        await ctx.refuse()
        await ctx.message.respond(Content.text('ask_usage', user=ctx.user))
        return
    try:
        ask_config = make_gen_config(
            system=Content.prompt('ask'), temperature=Gemini.ASK_TEMPERATURE,
        )
        contents = question
        # The same viewer's previous question: without it the model cannot make sense of
        # «а подробнее?» or «где ответ». Only a recent one – an old one is another topic
        previous = await get_last_tagged_interaction(ctx.user, ASK_TAG, ASK_FOLLOWUP_MINUTES)
        if previous:
            contents = Content.prompt(
                'ask_followup', question=question,
                previous_question=previous[0], previous_answer=previous[1],
            )
        text = await generate(contents, ask_config)
        await send_chunked(ctx, text, f'{ASK_TAG} {question}', max_chunks=ASK_MAX_CHUNKS)
    except Exception:
        logger.exception('Gemini !ask: ошибка для %s', ctx.user)
        await ctx.message.respond(Content.text('ask_error', user=ctx.user))


async def handle_summary(ctx: CommandContext) -> None:
    """The stream so far, or the previous stream (src/gemini/summary.py)."""
    previous = summary.mode(ctx.args, ctx.session_id) == summary.PREVIOUS

    async def run() -> bool:
        if previous:
            result = await summary.previous(ctx.session_id, ctx.user)
            tag, empty = '[summary] прошлый', 'summary_no_previous'
        else:
            result = await summary.now(ctx.session_id, ctx.user)
            tag, empty = '[summary]', 'summary_empty'
        if result is None:
            await ctx.refuse()
            await ctx.message.respond(Content.text(empty, user=ctx.user))
            return False
        return await send_chunked(ctx, result[0], tag)

    await _per_stream(ctx, summary.KIND, run, error='summary_error')


# Per-stream limits of !who, !versus and !summary: kind → (follower, VIP, subscriber
# or moderator), 0 – unlimited. The broadcaster is never limited; a subscribing VIP
# counts as a subscriber
def _limit_for(kind: str, chatter) -> int:
    if chatter.broadcaster:
        return 0
    follower, vip, sub = {
        who.WHO_KIND: (Who.PER_STREAM_FOLLOWER, Who.PER_STREAM_VIP, Who.PER_STREAM_SUB),
        who.VERSUS_KIND: (Who.PER_STREAM_FOLLOWER, Who.PER_STREAM_VIP, Who.PER_STREAM_SUB),
        summary.KIND: (Summary.PER_STREAM_FOLLOWER, Summary.PER_STREAM_VIP, Summary.PER_STREAM_SUB),
    }[kind]
    if chatter.moderator or chatter.subscriber or chatter.founder:
        return sub
    if chatter.vip:
        return vip
    return follower


# Who is waiting for an answer right now, per command. The limit is checked before
# generation and counted after sending, seconds later: a VIP's 10-second cooldown
# could let a second command through with the same count. Hence one at a time
_busy: dict[str, set[str]] = defaultdict(set)


async def _per_stream(ctx: CommandContext, kind: str, run, error: str = 'gen_failed') -> None:
    """A command under its per-stream limit, counted in bot_uses under `kind` like
    !ascii pictures. run() sends the answer and returns whether it reached chat:
    only that is counted, so a refusal costs nothing and a restart resets nothing."""
    busy = _busy[kind]
    if ctx.user in busy:
        # The first command is still being written and will answer itself
        await ctx.refuse()
        return
    # Taken before the first await, or two commands at once both pass the check
    busy.add(ctx.user)
    try:
        limit = _limit_for(kind, ctx.message.chatter)
        if limit and await count_bot_uses_this_stream(ctx.user, kind, ctx.session_id) >= limit:
            await ctx.refuse()
            await ctx.message.respond(Content.text(f'{kind}_no_left', user=ctx.user, limit=limit))
            return
        if await run():
            await record_bot_use(ctx.user, kind)
    except Exception:
        logger.exception('Gemini !%s: ошибка для %s', kind, ctx.user)
        await ctx.message.respond(Content.text(error, user=ctx.user))
    finally:
        busy.discard(ctx.user)


async def handle_who(ctx: CommandContext) -> None:
    """A handful of facts about the chatter from a fresh random sample of their
    whole history, less when Gemini blocks it."""
    args = ctx.args.split()
    target = clean_nick(args[0]) if args else ''
    if not target:
        await ctx.refuse()
        await ctx.message.respond(Content.text('who_usage', user=ctx.user))
        return

    async def run() -> bool:
        rungs = await who.who_rungs(ctx.user, target)
        if rungs is None:
            # Gemini was not called – a typo in the nick must not cost cooldown or quota
            await ctx.refuse()
            await ctx.message.respond(Content.text('who_unknown', user=ctx.user, target=target))
            return False
        text, _ = await walk(rungs, make_gen_config(), ctx.user)
        if await respond_and_save(ctx, text, who.who_tag(target), WHO_MAX):
            return True
        await ctx.message.respond(Content.text('who_failed', user=ctx.user, target=target))
        return False

    await _per_stream(ctx, who.WHO_KIND, run)


async def handle_versus(ctx: CommandContext) -> None:
    """Facts about two chatters piled up and mocked; whoever's are dumber loses."""
    args = ctx.args.split()
    nicks = list(dict.fromkeys(nick for nick in map(clean_nick, args) if nick))
    if len(nicks) < 2:
        await ctx.refuse()
        await ctx.message.respond(Content.text('versus_usage', user=ctx.user))
        return
    nick1, nick2 = nicks[0], nicks[1]

    async def run() -> bool:
        m1, m2 = await who.versus_material(nick1, nick2)
        if not (m1.known and m2.known):
            # Gemini was not called – a typo costs no cooldown or quota. With one side
            # unknown the model would make that person up, so it is a refusal as well
            await ctx.refuse()
            if not (m1.known or m2.known):
                text = Content.text('versus_unknown', user=ctx.user, nick1=nick1, nick2=nick2)
            else:
                text = Content.text('versus_unknown_one', user=ctx.user,
                                    target=nick2 if m1.known else nick1)
            await ctx.message.respond(text)
            return False
        rungs = await who.versus_rungs(ctx.user, m1, m2)
        text, _ = await walk(rungs, make_gen_config(), ctx.user)
        if await respond_and_save(ctx, text, who.versus_tag(nick1, nick2),
                                  max_chunks=VERSUS_MAX_CHUNKS):
            return True
        await ctx.message.respond(Content.text('versus_failed', user=ctx.user))
        return False

    await _per_stream(ctx, who.VERSUS_KIND, run)

