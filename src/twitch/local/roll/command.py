"""!roll – a free throw from chat, !rollstat – how the game stands for you or another player."""
import logging
import math

from src.twitch.core.commands import CommandContext
from src.core.config import Roll
from src.core.content import Content
from src.twitch.core.limits import deny
from src.twitch.core.viewer import Tier, sub_hint, tier_of
from src.twitch.core.replies import reply
from src.twitch.local.roll import board, game, overlay, rules
from src.twitch.local.roll.storage import get_tier
from src.twitch.local.roll.texts import (
    champion_note, compose, curse_note, input_preview, loser_note, luck_values, mass_values, out_of, reward_title,
    split_message,
)

logger = logging.getLogger(__name__)

# Cooldown scope of the «зачилься» refusal: said once, then every !roll is dropped
# silently until the spam pause ends
PAUSE_SCOPE = 'roll_pause'


def free_limit_for(chatter) -> int:
    """How many free throws a viewer gets by their badges. The broadcaster rolls without
    limit, a non-follower never reaches the game."""
    return rules.free_limit(tier_of(chatter))


async def handle_roll(ctx: CommandContext) -> None:
    if await _roll(ctx):
        # The month's first stream: the previous month's results, once, to a chat that is playing
        await board.announce(ctx.bot, ctx.session_id)
    elif ctx.bot.stream_live:
        # A refused throw in a stream shows no card, but it still brings the titles back
        overlay.publish(overlay.wake())


async def _roll(ctx: CommandContext) -> bool:
    """The throw and its reply. True – the throw reached the stream as a card."""
    if ctx.args:
        # «!roll 5», «!roll @nick»: a throw takes nothing, and silence reads as a broken bot
        await ctx.refuse()
        await reply(ctx.message, Content.text(
            'roll_bad_input', user=ctx.user, input=input_preview(ctx.original_args),
        ))
        return False
    if not ctx.bot.stream_live:
        # The game lives inside a stream: no throws are accepted without one
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('roll_offline', user=ctx.user))
        return False
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
        # Once per 30 s, like every limit refusal: a subscriber or a moderator has no
        # cooldown and would otherwise get it for every further !roll
        await deny(ctx.bot, ctx.message, ctx.user, text + sub_hint(ctx.message.chatter, limit, Roll.FREE_SUB))
        return False
    if result.status == game.Status.UNDER_FLASK:
        # Nothing was thrown, so nothing to wait off; said once per 30 s like a limit refusal
        ctx.clear_cooldown()
        await deny(ctx.bot, ctx.message, ctx.user, Content.text(
            'roll_under_flask', user=ctx.user, minutes=result.wait_minutes,
        ))
        return False
    if result.status == game.Status.TOO_FAST:
        # Nothing was thrown, so nothing to wait off. One refusal per pause: a subscriber
        # has no cooldown and would otherwise get one for every message
        ctx.clear_cooldown()
        if ctx.bot.cooldown_remaining(ctx.user, PAUSE_SCOPE):
            return False
        ctx.bot.set_cooldown(ctx.user, math.ceil(result.pause_seconds), PAUSE_SCOPE)
        await reply(ctx.message, Content.text(
            'roll_too_fast', user=ctx.user, minutes=result.wait_minutes,
        ))
        return False
    if result.champion is None:
        logger.error('Ролл сохранён, но титулы сессии %s не найдены', ctx.session_id)
        await reply(ctx.message, Content.text('roll_error', user=ctx.user))
        return False
    overlay.publish(overlay.thrown('roll', ctx.user, None, result))
    # For a cursed player the «из» (out of) is their ceiling, not the roll's upper bound
    cursed = result.ceiling is not None
    loser = ''
    if result.loser is not None and result.loser[0] == ctx.user:
        key = 'roll_cursed_self' if cursed else 'roll_loser_self'
    else:
        key = 'roll_cursed_other' if cursed else 'roll_loser_other'
        loser = loser_note(result)
    ceiling, top = out_of(result)
    text = Content.text(key, user=ctx.user, value=result.value, ceiling=ceiling, max=top)
    free_left = (
        '' if result.free_left is None
        else Content.text('roll_free_left', free_left=result.free_left)
    )
    note = curse_note(result)
    await reply(ctx.message, compose(text, loser, champion_note(result), free_left, note))
    return True


async def handle_rollstat(ctx: CommandContext) -> None:
    """A player's roll, free throws left and effects, and the session's two titles: your own,
    or another player's with «!rollstat ник». No Gemini, no throw: reading changes nothing."""
    target = ctx.user
    if ctx.args:
        target = rules.parse_nick(ctx.original_args)
        if target is None:
            # «!rollstat два слова»: only a nick may follow, and silence reads as a broken bot
            await ctx.refuse()
            await reply(ctx.message, Content.text(
                'rollstat_bad_input', user=ctx.user, input=input_preview(ctx.original_args),
            ))
            return
    if not ctx.bot.stream_live:
        # The game lives inside a stream, and outside one there is nothing to show
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('roll_offline', user=ctx.user))
        return
    overlay.publish(overlay.wake())
    # Another player's texts speak of them, not to them: the keys with _other
    other = target != ctx.user
    suffix = '_other' if other else ''
    if other:
        # No badges for someone who did not write this message: their status as chat last wrote it
        tier = Tier(await get_tier(ctx.session_id, target) or Tier.REGULAR)
        limit, unlimited = rules.free_limit(tier), tier == Tier.BROADCASTER
    else:
        limit, unlimited = free_limit_for(ctx.message.chatter), ctx.message.chatter.broadcaster
    standing = await game.status(ctx.session_id, target, limit=limit, unlimited=unlimited)
    if standing.value is None:
        own = Content.text(f'rollstat_none{suffix}', user=ctx.user, target=target)
    elif standing.ceiling is not None:
        # Under АГОНИЯ the next throw is capped lower than a curse ever goes: the lower one is said
        ceiling = standing.ceiling if standing.agony_left is None else min(standing.ceiling, rules.agony_throw_top())
        own = Content.text(f'rollstat_cursed{suffix}', user=ctx.user, target=target, value=standing.value,
                           max=Roll.MAX, ceiling=ceiling)
    else:
        own = Content.text(f'rollstat_self{suffix}', user=ctx.user, target=target, value=standing.value,
                           max=Roll.MAX)
    parts = [own]
    if standing.free_left is not None:
        parts.append(Content.text('roll_free_left', free_left=standing.free_left))
    if standing.curse_power is not None:
        parts.append(Content.text(
            'rollstat_curse_doubled', power=standing.curse_power, step=rules.curse_step(standing.curse_power),
            hold=round(rules.curse_hold(standing.curse_power) / 60),
        ))
    if standing.curse_minutes_left is not None:
        parts.append(Content.text(f'rollstat_curse_hold{suffix}', target=target, minutes=standing.curse_minutes_left))
    if standing.shield_left is not None:
        parts.append(Content.text('rollstat_shield_left', minutes=standing.shield_left))
    elif standing.shield_minutes_left is not None:
        parts.append(Content.text('rollstat_perk_shield', minutes=standing.shield_minutes_left))
    if standing.flask_left is not None:
        parts.append(Content.text(f'rollstat_flask{suffix}', target=target, minutes=standing.flask_left))
    # The luck and the choke are as strong as the mass flask holding the game makes them now
    mode = 'agony' if standing.agony_left is not None else 'blessing' if standing.blessed_left is not None else None
    if standing.luck_left is not None and standing.ceiling is not None:
        # The curse beats the luck: its minutes run, but no lucky throw comes until the curse lifts
        parts.append(Content.text('rollstat_luck_cursed', minutes=standing.luck_left))
    elif standing.luck_left is not None:
        parts.append(Content.text('rollstat_luck', minutes=standing.luck_left, chance=standing.luck_next,
                                  max=rules.luck_of(mode, standing.luck_stacks or 1).top,
                                  stacks=standing.luck_stacks, **luck_values(mode, standing.luck_stacks)))
    if standing.choke_left is not None:
        parts.append(Content.text(f'rollstat_choke{suffix}', target=target, minutes=standing.choke_left,
                                  stacks=standing.choke_stacks,
                                  choke_step=rules.choke_step(mode) * (standing.choke_stacks or 1)))
    if standing.blessed_left is not None:
        parts.append(Content.text('rollstat_blessed', minutes=standing.blessed_left))
    if standing.agony_left is not None:
        parts.append(Content.text('rollstat_agony', minutes=standing.agony_left, **mass_values()))
    if standing.champion is None:
        parts.append(Content.text('rollstat_nobody'))
    else:
        # A lone player is the champion, and there is no loser yet
        if standing.loser is not None:
            loser, loser_val = standing.loser
            parts.append(Content.text('rollstat_loser', loser=loser, loser_val=loser_val, max=Roll.MAX))
        parts.append(champion_note(game.Outcome(game.Status.OK, champion=standing.champion)))
    # Many effects at once can pass Twitch's limit: the answer goes out in as many messages as it needs
    for piece in split_message(compose(*parts)):
        await reply(ctx.message, piece)
