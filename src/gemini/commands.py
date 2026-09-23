"""Commands that call Gemini."""
import functools
import logging


from src.core.commands import CommandContext
from src.core.config import Gemini, Summary, Who
from src.core.content import Content
from src.core.database import get_last_tagged_interaction
from src.core.utils import clean_nick, reply, reply_to_bot
from src.core.viewer import by_tier, tier_of
from src.gemini import summary, who
from src.gemini.answer_context import Question, answer
from src.gemini.client import generate, make_gen_config
from src.gemini.ladder import walk
from src.gemini.limits import PerStreamLimit
from src.gemini.output import WHO_MAX
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
    except Exception:
        logger.exception('Gemini: ошибка генерации для %s', ctx.user)
        await reply(ctx.message, Content.text('gen_error', user=ctx.user))
        return
    if not await respond_and_save(ctx, text, ctx.prompt):
        logger.warning('Ответ не отправлен для %s, запрос: %s', ctx.user, ctx.prompt[:100])
        await reply(ctx.message, Content.text('no_answer', user=ctx.user))


async def handle_ask(ctx: CommandContext) -> None:
    # The question in its original case: «РФ», names and code lose their meaning without it
    question = ctx.original_args
    if not question:
        await ctx.refuse()
        await reply(ctx.message, Content.text('ask_usage', user=ctx.user))
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
    except Exception:
        logger.exception('Gemini !ask: ошибка для %s', ctx.user)
        await reply(ctx.message, Content.text('ask_error', user=ctx.user))
        return
    await send_chunked(ctx, text, f'{ASK_TAG} {question}', max_chunks=ASK_MAX_CHUNKS)


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
            await reply(ctx.message, Content.text(empty, user=ctx.user))
            return False
        return await send_chunked(ctx, result[0], tag)

    await LIMITS[summary.KIND].run(ctx, run)


# Per-stream limits of !who, !versus and !summary: kind → (follower, VIP, subscriber
# or moderator), 0 – unlimited. The broadcaster is never limited; a subscribing VIP
# counts as a subscriber
def _limit_for(kind: str, chatter) -> int:
    follower, vip, sub = {
        who.WHO_KIND: (Who.PER_STREAM_FOLLOWER, Who.PER_STREAM_VIP, Who.PER_STREAM_SUB),
        who.VERSUS_KIND: (Who.PER_STREAM_FOLLOWER, Who.PER_STREAM_VIP, Who.PER_STREAM_SUB),
        summary.KIND: (Summary.PER_STREAM_FOLLOWER, Summary.PER_STREAM_VIP, Summary.PER_STREAM_SUB),
    }[kind]
    return by_tier(tier_of(chatter), broadcaster=0, sub=sub, vip=vip, regular=follower)


# One limit per command. The limit function reads the config on every call
LIMITS = {
    who.WHO_KIND: PerStreamLimit(who.WHO_KIND, functools.partial(_limit_for, who.WHO_KIND), 'gen_failed'),
    who.VERSUS_KIND: PerStreamLimit(who.VERSUS_KIND, functools.partial(_limit_for, who.VERSUS_KIND), 'gen_failed'),
    summary.KIND: PerStreamLimit(summary.KIND, functools.partial(_limit_for, summary.KIND), 'summary_error'),
}


async def handle_who(ctx: CommandContext) -> None:
    """A handful of facts about the chatter from a fresh random sample of their
    whole history, less when Gemini blocks it."""
    args = ctx.args.split()
    target = clean_nick(args[0]) if args else ''
    if not target:
        await ctx.refuse()
        await reply(ctx.message, Content.text('who_usage', user=ctx.user))
        return

    async def run() -> bool:
        rungs = await who.who_rungs(ctx.user, target)
        if rungs is None:
            # Gemini was not called – a typo in the nick must not cost cooldown or quota
            await ctx.refuse()
            await reply(ctx.message, Content.text('who_unknown', user=ctx.user, target=target))
            return False
        text, _ = await walk(rungs, make_gen_config(), ctx.user)
        if await respond_and_save(ctx, text, who.who_tag(target), WHO_MAX):
            return True
        await reply(ctx.message, Content.text('who_failed', user=ctx.user, target=target))
        return False

    await LIMITS[who.WHO_KIND].run(ctx, run)


async def handle_versus(ctx: CommandContext) -> None:
    """Facts about two chatters piled up and mocked; whoever's are dumber loses."""
    args = ctx.args.split()
    nicks = list(dict.fromkeys(nick for nick in map(clean_nick, args) if nick))
    if len(nicks) < 2:
        await ctx.refuse()
        await reply(ctx.message, Content.text('versus_usage', user=ctx.user))
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
            await reply(ctx.message, text)
            return False
        rungs = await who.versus_rungs(ctx.user, m1, m2)
        text, _ = await walk(rungs, make_gen_config(), ctx.user)
        if await respond_and_save(ctx, text, who.versus_tag(nick1, nick2),
                                  max_chunks=VERSUS_MAX_CHUNKS):
            return True
        await reply(ctx.message, Content.text('versus_failed', user=ctx.user))
        return False

    await LIMITS[who.VERSUS_KIND].run(ctx, run)

