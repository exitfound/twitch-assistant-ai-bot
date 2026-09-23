"""!roll – a free throw from chat, !rollstat – how the game stands."""
import logging

from src.core.commands import CommandContext
from src.core.config import Roll
from src.core.content import Content
from src.core.viewer import by_tier, tier_of
from src.core.utils import reply
from src.local.roll import game
from src.local.roll.texts import champion_note, curse_note, input_preview, reward_title

logger = logging.getLogger(__name__)


def free_limit_for(chatter) -> int:
    """How many free throws a viewer gets by their badges.

    Same ladder as the cooldown and the quota: a subscription or moderator status
    gives the most, VIP the middle, everyone else the base limit. The broadcaster
    rolls without limit, a non-follower never reaches the game.
    """
    # The broadcaster's value is only stored for extra, which compares throws with it
    return by_tier(tier_of(chatter), sub=Roll.FREE_SUB, vip=Roll.FREE_VIP,
                   regular=Roll.FREE_PER_SESSION, broadcaster=Roll.FREE_PER_SESSION)


async def handle_roll(ctx: CommandContext) -> None:
    if ctx.args:
        # «!roll 5», «!roll @nick»: a throw takes nothing, and silence reads as a broken bot
        await ctx.refuse()
        await reply(ctx.message, Content.text(
            'roll_bad_input', user=ctx.user, input=input_preview(ctx.original_args),
        ))
        return
    if not ctx.bot.stream_live:
        # The game lives inside a stream: no throws are accepted without one
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('roll_offline', user=ctx.user))
        return
    # The broadcaster rolls without limit and without the free-throws-left note,
    # everyone else gets a limit by their badges
    limit = free_limit_for(ctx.message.chatter)
    result = await game.free_throw(
        ctx.session_id, ctx.user, limit=limit, unlimited=ctx.message.chatter.broadcaster,
    )
    if result.status == game.Status.NO_FREE_LEFT:
        if ctx.bot.rewards_active:
            text = Content.text(
                'roll_no_free_reward', user=ctx.user, limit=limit,
                reward=reward_title(game.Action.EXTRA),
            )
        else:
            text = Content.text('roll_no_free', user=ctx.user, limit=limit)
        await reply(ctx.message, text)
        return
    if result.loser is None:
        logger.error('Ролл сохранён, но лузер сессии %s не найден', ctx.session_id)
        await reply(ctx.message, Content.text('roll_error', user=ctx.user))
        return
    loser_name, loser_val = result.loser
    # For a cursed player the «из» (out of) is their ceiling, not the roll's upper bound
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
    await reply(ctx.message, ' '.join(filter(None, (text, champion_note(result), free_left, note))))


async def handle_rollstat(ctx: CommandContext) -> None:
    """Your own roll, your free throws left and the session's two titles. No Gemini,
    no throw: reading your standing changes nothing."""
    if not ctx.bot.stream_live:
        # The game lives inside a stream, and outside one there is nothing to show
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('roll_offline', user=ctx.user))
        return
    chatter = ctx.message.chatter
    standing = await game.status(
        ctx.session_id, ctx.user,
        limit=free_limit_for(chatter), unlimited=chatter.broadcaster,
    )
    if standing.value is None:
        own = Content.text('rollstat_none', user=ctx.user)
    elif standing.ceiling is not None:
        own = Content.text('rollstat_cursed', user=ctx.user, value=standing.value,
                           max=Roll.MAX, ceiling=standing.ceiling)
    else:
        own = Content.text('rollstat_self', user=ctx.user, value=standing.value, max=Roll.MAX)
    parts = [own]
    if standing.free_left is not None:
        parts.append(Content.text('roll_free_left', free_left=standing.free_left))
    if standing.curse_minutes_left is not None:
        parts.append(Content.text('rollstat_curse_hold', minutes=standing.curse_minutes_left))
    if standing.shield_left is not None:
        parts.append(Content.text('rollstat_shield_left', minutes=standing.shield_left))
    elif standing.shield_minutes_left is not None:
        parts.append(Content.text('rollstat_perk_shield', minutes=standing.shield_minutes_left))
    if standing.loser is None:
        parts.append(Content.text('rollstat_nobody'))
    else:
        loser, loser_val = standing.loser
        parts.append(Content.text('rollstat_loser', loser=loser, loser_val=loser_val, max=Roll.MAX))
        if standing.champion is not None:
            parts.append(champion_note(game.Outcome(game.Status.OK, champion=standing.champion)))
    await reply(ctx.message, ' '.join(filter(None, parts)))
