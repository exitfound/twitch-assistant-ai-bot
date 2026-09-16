"""!roll — бесплатный бросок из чата."""
import logging

from src.core.commands import CommandContext
from src.core.config import Roll
from src.core.content import Content
from src.local.roll import game
from src.local.roll.texts import champion_note, curse_note, reward_title

logger = logging.getLogger(__name__)


async def handle_roll(ctx: CommandContext) -> None:
    if not ctx.bot.stream_live:
        # Игра живёт в рамках эфира: без стрима броски не принимаются
        ctx.clear_cooldown()
        await ctx.message.respond(Content.text('roll_offline', user=ctx.user))
        return
    # Стример катает без лимита и без приписки об остатке бесплатных
    result = await game.free_throw(
        ctx.session_id, ctx.user, unlimited=ctx.message.chatter.broadcaster,
    )
    if result.status == game.NO_FREE_LEFT:
        if ctx.bot.rewards_active:
            text = Content.text(
                'roll_no_free_reward', user=ctx.user, limit=Roll.FREE_PER_SESSION,
                reward=reward_title(game.ACTION_EXTRA),
            )
        else:
            text = Content.text('roll_no_free', user=ctx.user, limit=Roll.FREE_PER_SESSION)
        await ctx.message.respond(text)
        return
    if result.loser is None:
        logger.error('Ролл сохранён, но лузер сессии %s не найден', ctx.session_id)
        await ctx.message.respond(Content.text('roll_error', user=ctx.user))
        return
    loser_name, loser_val = result.loser
    # У проклятого «из» — это его потолок, а не верхняя граница ролла
    cursed = result.ceiling is not None
    if loser_name == ctx.user:
        key = 'roll_cursed_self' if cursed else 'roll_loser_self'
    else:
        key = 'roll_cursed_other' if cursed else 'roll_loser_other'
    text = Content.text(
        key, user=ctx.user, value=result.value, ceiling=result.ceiling,
        loser=loser_name, loser_val=loser_val, max=Roll.MAX,
    )
    free_left = (
        '' if result.free_left is None
        else Content.text('roll_free_left', free_left=result.free_left)
    )
    note = curse_note(result)
    await ctx.message.respond(' '.join(filter(None, (text, champion_note(result), free_left, note))))
