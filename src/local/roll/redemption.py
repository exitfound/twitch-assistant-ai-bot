"""Ответ на выкуп награды за баллы канала.

Twitch-сторона – создание наград, подтверждение, возврат баллов – живёт в
src/local/roll/rewards.py. Здесь только игра: применить награду через game и
сказать в чат, что вышло.
"""
import logging

from src.core.config import Roll
from src.core.content import Content
from src.core.database import save_bot_interaction
from src.local.roll import game
from src.local.roll.texts import champion_note, curse_note, curse_values, reward_title

logger = logging.getLogger(__name__)

# Сколько символов из поля награды цитировать в отказе: туда пишут что угодно
INPUT_PREVIEW_CHARS = 25

_DONE = {
    game.ACTION_EXTRA: 'reward_extra_done',
    game.ACTION_REROLL: 'reward_reroll_done',
    game.ACTION_CURSE: 'reward_curse_hit',
    game.ACTION_SHIELD: 'reward_shield_done',
}

_REFUND = {
    game.FREE_LEFT: 'reward_refund_free_left',
    game.BAD_TARGET: 'reward_refund_bad_target',
    game.SELF_TARGET: 'reward_refund_self',
    game.NOT_ROLLED: 'reward_refund_not_rolled',
    game.SHIELDED: 'reward_refund_shielded',
    game.ALREADY_SHIELDED: 'reward_refund_shield_active',
    game.ALREADY_CURSED: 'reward_refund_already_cursed',
    game.PROTECTED: 'reward_refund_protected',
    game.UNKNOWN_TARGET: 'reward_refund_unknown_target',
    game.PERK_SHIELDED: 'reward_refund_perk_shield',
}

# После этих наград к тексту дописывается хвост про проклятие цели
_WITH_CURSE_NOTE = (game.ACTION_EXTRA, game.ACTION_REROLL)


async def handle_redemption(
    bot, action: str, redemption_id: str, user: str, user_input: str,
) -> bool | None:
    """Применить награду и сообщить в чат.

    Возвращает, что сделать с выкупом в Twitch: True – подтвердить,
    False – вернуть баллы, None – это повтор уже обработанного выкупа,
    статус не трогать.
    """
    session_id = bot.session_id
    if not bot.stream_live:
        # Без эфира награды на паузе, но выкуп мог проскочить в момент конца стрима
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
    if outcome.status == game.DUPLICATE:
        logger.info('Повтор выкупа %s, пропускаю', redemption_id)
        return None
    await _say(bot, session_id, action, _render(action, user, user_input, outcome))
    return outcome.ok


def _render(action: str, user: str, user_input: str, outcome: game.Outcome) -> str:
    if outcome.ok:
        key = _DONE[action]
        # У проклятого «из» – это его потолок, а не верхняя граница ролла
        if action == game.ACTION_EXTRA and outcome.ceiling is not None:
            key = 'reward_extra_cursed'
        # Цель ещё не катала: «было – стало» писать не про что
        elif action == game.ACTION_REROLL and outcome.old_value is None:
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
    # Щит ролл не меняет – китежанина и проклятие к нему не дописываем
    if outcome.ok and action != game.ACTION_SHIELD:
        note = ''
        if action in _WITH_CURSE_NOTE:
            note = curse_note(outcome)
        text = ' '.join(filter(None, (text, champion_note(outcome), note)))
    return text


async def _say(bot, session_id: str, action: str, text: str) -> None:
    # Итог награды уже применён: если сообщение не ушло, баллы это не меняет
    if text and await bot.send_chat_message(text):
        await save_bot_interaction(session_id, '_reward_', f'[reward:{action}]', text)
