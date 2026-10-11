"""Reply to a channel-points reward redemption.

The Twitch side – creating rewards, fulfilling, refunding points – lives in
src/twitch/local/roll/rewards.py. Only the game is here: apply the reward through game and
say in chat what came of it.
"""
import logging
from collections.abc import Awaitable, Callable

from src.core.config import Follow, Rewards, Roll, Twitch
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.twitch.core.port import BotPort
from src.twitch.core.viewer import Tier, tier_sub_hint
from src.twitch.local.roll import game, overlay
from src.twitch.local.roll.storage import get_action_status, get_tier
from src.twitch.local.roll.texts import (
    champion_note, compose, curse_note, curse_values, input_preview, reward_title,
)

logger = logging.getLogger(__name__)

_DONE = {
    game.Action.EXTRA: 'reward_extra_done',
    game.Action.REROLL: 'reward_reroll_done',
    game.Action.CURSE: 'reward_curse_hit',
    game.Action.SHIELD: 'reward_shield_up',
    game.Action.CLEANSE: 'reward_cleanse_done',
}

_REFUND = {
    game.Status.FREE_LEFT: 'reward_refund_free_left',
    game.Status.TOO_FAST: 'reward_refund_too_fast',
    game.Status.REROLL_LIMIT: 'reward_refund_reroll_limit',
    game.Status.SHIELD_LIMIT: 'reward_refund_shield_limit',
    game.Status.CURSE_LIMIT: 'reward_refund_curse_limit',
    game.Status.CLEANSE_LIMIT: 'reward_refund_cleanse_limit',
    game.Status.BAD_TARGET: 'reward_refund_bad_target',
    game.Status.EXTRA_WORDS: 'reward_refund_extra_words',
    game.Status.SELF_TARGET: 'reward_refund_self',
    game.Status.NOT_ROLLED: 'reward_refund_not_rolled',
    game.Status.SHIELDED: 'reward_refund_shield_holds',
    game.Status.ALREADY_SHIELDED: 'reward_refund_shield_still_up',
    game.Status.ALREADY_CURSED: 'reward_refund_already_cursed',
    game.Status.PROTECTED: 'reward_refund_protected',
    game.Status.UNKNOWN_TARGET: 'reward_refund_unknown_target',
    game.Status.PERK_SHIELDED: 'reward_refund_perk_shield',
    game.Status.NOT_CURSED: 'reward_refund_not_cursed',
    game.Status.CLEANSED: 'reward_refund_cleansed',
}

# Refusals by the buyer's limit: a subscription would raise it, and the refusal says so
_LIMITS = (game.Status.REROLL_LIMIT, game.Status.SHIELD_LIMIT, game.Status.CURSE_LIMIT,
           game.Status.CLEANSE_LIMIT)

# After these rewards a note about the target's curse is appended to the text
_WITH_CURSE_NOTE = (game.Action.EXTRA, game.Action.REROLL)


async def handle_redemption(
    bot: BotPort, action: str, redemption_id: str, user: str, user_input: str,
    *, follows: Callable[[], Awaitable[bool]],
) -> bool | None:
    """Apply the reward and report it in chat.

    Returns what to do with the redemption on Twitch: True – fulfill,
    False – refund the points, None – a repeat of an already handled redemption,
    leave the status alone. follows() asks Twitch whether the buyer follows the channel.
    """
    session_id = bot.session_id
    if await get_action_status(redemption_id) is not None:
        # Checked before the offline refund: EventSub may redeliver an applied redemption
        # after the stream ended, and chat must not hear «points refunded» for it
        logger.info('Повтор выкупа %s, пропускаю', redemption_id)
        return None
    try:
        if not bot.stream_live:
            # Offline the rewards are paused, but a redemption may slip through as the stream ends
            return await _refuse(bot, session_id, action, user, user_input, redemption_id,
                                 game.Status.OFFLINE, 'reward_refund_offline')
        if not await _may_play(session_id, user, follows):
            # Points accrue without a follow, and the game is closed to a non-follower in chat
            return await _refuse(bot, session_id, action, user, user_input, redemption_id,
                                 game.Status.NOT_FOLLOWER, 'reward_refund_follow')
        outcome = await game.redeem(action, session_id, user, user_input, redemption_id)
    except Exception:
        logger.exception('Награда %s от %s не применена', action, user)
        await _say(bot, session_id, action, Content.text(
            'reward_error', user=user, reward=reward_title(action),
        ))
        return False
    if outcome.status == game.Status.DUPLICATE:
        logger.info('Повтор выкупа %s, пропускаю', redemption_id)
        return None
    # A refusal has no card, but the purchase still brings the titles back
    overlay.publish(overlay.redeemed(action, user, outcome) or overlay.wake())
    text = _render(action, user, user_input, outcome)
    if outcome.status in _LIMITS:
        text += await _sub_hint(session_id, user, outcome.limit)
    await _say(bot, session_id, action, text)
    return outcome.ok


async def _refuse(bot: BotPort, session_id: str, action: str, user: str, user_input: str,
                  redemption_id: str, status: game.Status, key: str) -> bool | None:
    """A refusal the game never saw, journaled so a redelivery is skipped: False – refund,
    None – already refused."""
    if not await game.refuse(action, session_id, user, user_input, redemption_id, status):
        logger.info('Повтор выкупа %s, пропускаю', redemption_id)
        return None
    await _say(bot, session_id, action, Content.text(key, user=user, reward=reward_title(action)))
    return False


async def _sub_hint(session_id: str, user: str, limit: int) -> str:
    """The subscription hint by the buyer's status in chat. Never raises: the outcome is
    applied by now, and a missing hint must not keep the redemption without a status."""
    try:
        return tier_sub_hint(Tier(await get_tier(session_id, user) or Tier.REGULAR), limit, Rewards.LIMIT_SUB)
    except Exception:
        logger.exception('Приписка про сабку для %s не собрана', user)
        return ''


async def _may_play(session_id: str, user: str, follows: Callable[[], Awaitable[bool]]) -> bool:
    """Whether the buyer may play: the same follow gate as !roll in chat.

    A redemption carries no badges: the broadcaster is known by the channel's login, and a
    moderator, subscriber or VIP by the status chat last showed this stream (roll_tiers).
    """
    if not Follow.REQUIRED or user == (Twitch.CHANNEL or '').lower():
        return True
    tier = await get_tier(session_id, user)
    if tier is not None and tier != Tier.REGULAR:
        return True
    return await follows()


def _render(action: str, user: str, user_input: str, outcome: game.Outcome) -> str:
    if outcome.ok:
        key = _DONE[action]
        # For a cursed player the «из» (out of) is their ceiling, not the roll's upper bound
        if action == game.Action.EXTRA and outcome.ceiling is not None:
            key = 'reward_extra_cursed'
        # The target has not rolled yet: there is no «было – стало» (before – after) to write
        elif action == game.Action.REROLL and outcome.old_value is None:
            key = 'reward_reroll_first'
        elif action == game.Action.CLEANSE:
            # value is set only when the cleanse returned the roll the curse threw over
            yourself = outcome.target == user
            if outcome.value is not None:
                key = 'reward_cleanse_self_restored' if yourself else 'reward_cleanse_restored'
            elif yourself:
                key = 'reward_cleanse_self'
    else:
        key = _REFUND.get(outcome.status, 'reward_error')
    loser, loser_val = outcome.loser or ('', '')
    text = Content.text(
        key, user=user, reward=reward_title(action), target=outcome.target,
        input=input_preview(user_input),
        old=outcome.old_value, value=outcome.value, free_left=outcome.free_left,
        shield=Rewards.SHIELD_MINUTES,
        loser=loser, loser_val=loser_val, max=Roll.MAX,
        ceiling=outcome.ceiling, next=outcome.next_ceiling,
        minutes=outcome.curse_minutes_left, protect=outcome.protect_minutes_left,
        limit=outcome.limit, window=Rewards.REROLL_WINDOW_MINUTES,
        **curse_values(),
    )
    # A shield and a cleanse that returned nothing do not change the roll – no champion
    # or curse note is appended
    changed = action != game.Action.CLEANSE or outcome.value is not None
    if outcome.ok and action != game.Action.SHIELD and changed:
        note = ''
        if action in _WITH_CURSE_NOTE:
            note = curse_note(outcome)
        text = compose(text, champion_note(outcome), note)
    return text


async def _say(bot: BotPort, session_id: str, action: str, text: str) -> None:
    # The reward outcome is already applied: a message that did not go out or was not
    # recorded does not change anything about the points, and must not keep the caller
    # from setting the redemption's status
    try:
        if text and await bot.send_chat_message(text):
            await save_bot_interaction(session_id, '_reward_', f'[reward:{action}]', text)
    except Exception:
        logger.exception('Итог награды %s не записан', action)
