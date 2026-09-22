"""Reply to a channel-points reward redemption.

The Twitch side – creating rewards, fulfilling, refunding points – lives in
src/local/roll/rewards.py. Only the game is here: apply the reward through game and
say in chat what came of it.
"""
import logging

from src.core.config import Roll
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.core.port import BotPort
from src.local.roll import game
from src.local.roll.storage import get_action_status
from src.local.roll.texts import champion_note, curse_note, curse_values, reward_title

logger = logging.getLogger(__name__)

# How many characters of the reward input to quote in a refusal: people write anything there
INPUT_PREVIEW_CHARS = 25

_DONE = {
    game.Action.EXTRA: 'reward_extra_done',
    game.Action.REROLL: 'reward_reroll_done',
    game.Action.CURSE: 'reward_curse_hit',
    game.Action.SHIELD: 'reward_shield_done',
}

_REFUND = {
    game.Status.FREE_LEFT: 'reward_refund_free_left',
    game.Status.BAD_TARGET: 'reward_refund_bad_target',
    game.Status.SELF_TARGET: 'reward_refund_self',
    game.Status.NOT_ROLLED: 'reward_refund_not_rolled',
    game.Status.SHIELDED: 'reward_refund_shielded',
    game.Status.ALREADY_SHIELDED: 'reward_refund_shield_active',
    game.Status.ALREADY_CURSED: 'reward_refund_already_cursed',
    game.Status.PROTECTED: 'reward_refund_protected',
    game.Status.UNKNOWN_TARGET: 'reward_refund_unknown_target',
    game.Status.PERK_SHIELDED: 'reward_refund_perk_shield',
}

# After these rewards a note about the target's curse is appended to the text
_WITH_CURSE_NOTE = (game.Action.EXTRA, game.Action.REROLL)


async def handle_redemption(
    bot: BotPort, action: str, redemption_id: str, user: str, user_input: str,
) -> bool | None:
    """Apply the reward and report it in chat.

    Returns what to do with the redemption on Twitch: True – fulfill,
    False – refund the points, None – a repeat of an already handled redemption,
    leave the status alone.
    """
    session_id = bot.session_id
    if await get_action_status(redemption_id) is not None:
        # Checked before the offline refund: EventSub may redeliver an applied redemption
        # after the stream ended, and chat must not hear «points refunded» for it
        logger.info('Повтор выкупа %s, пропускаю', redemption_id)
        return None
    if not bot.stream_live:
        # Offline the rewards are paused, but a redemption may slip through as the stream ends
        await _say(bot, session_id, action, Content.text(
            'reward_refund_offline', user=user, reward=reward_title(action),
        ))
        return False
    try:
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
    await _say(bot, session_id, action, _render(action, user, user_input, outcome))
    return outcome.ok


def _render(action: str, user: str, user_input: str, outcome: game.Outcome) -> str:
    if outcome.ok:
        key = _DONE[action]
        # For a cursed player the «из» (out of) is their ceiling, not the roll's upper bound
        if action == game.Action.EXTRA and outcome.ceiling is not None:
            key = 'reward_extra_cursed'
        # The target has not rolled yet: there is no «было – стало» (before – after) to write
        elif action == game.Action.REROLL and outcome.old_value is None:
            key = 'reward_reroll_first'
    else:
        key = _REFUND.get(outcome.status, 'reward_error')
    loser, loser_val = outcome.loser or ('', '')
    text = Content.text(
        key, user=user, reward=reward_title(action), target=outcome.target,
        input=user_input.strip()[:INPUT_PREVIEW_CHARS],
        old=outcome.old_value, value=outcome.value, free_left=outcome.free_left,
        loser=loser, loser_val=loser_val, max=Roll.MAX,
        ceiling=outcome.ceiling, next=outcome.next_ceiling,
        minutes=outcome.curse_minutes_left, protect=outcome.protect_minutes_left,
        **curse_values(),
    )
    # A shield does not change the roll – no champion or curse note is appended to it
    if outcome.ok and action != game.Action.SHIELD:
        note = ''
        if action in _WITH_CURSE_NOTE:
            note = curse_note(outcome)
        text = ' '.join(filter(None, (text, champion_note(outcome), note)))
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
