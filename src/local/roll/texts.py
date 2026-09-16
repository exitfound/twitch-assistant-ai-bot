"""Общие куски сообщений игры: названия наград, хвосты про китежанина и проклятие.

Нужны и ответу на !roll, и итогам наград, и описаниям наград в Twitch.
"""
from src.core.config import Rewards, Roll
from src.core.content import Content
from src.local.roll.game import Outcome


def reward_title(action: str) -> str:
    return Content.text(f'reward_{action}_title')


def curse_values() -> dict:
    """Параметры проклятия для текстов: в описании награды и в чате."""
    return {
        'step': Rewards.CURSE_STEP, 'floor': Rewards.CURSE_FLOOR,
        'hold': Rewards.CURSE_HOLD_MINUTES,
    }


def curse_note(outcome: Outcome) -> str:
    """Хвост к сообщению о броске проклятого. Пусто, если проклятия нет.

    Любой бросок по проклятому опускает потолок, поэтому хвост всегда про
    следующий потолок — или про минуты до снятия, если потолок уже на дне.
    """
    if outcome.ceiling is None:
        return ''
    key = 'roll_curse_hold' if outcome.curse_minutes_left is not None else 'roll_curse_step'
    return Content.text(
        key, ceiling=outcome.ceiling, next=outcome.next_ceiling,
        minutes=outcome.curse_minutes_left, **curse_values(),
    )


def champion_note(outcome: Outcome) -> str:
    """«Китежанин стрима: …» к сообщению о броске. Пусто, если показывать некого."""
    if outcome.champion is None:
        return ''
    name, value = outcome.champion
    return Content.text('roll_champion', champion=name, champion_val=value, max=Roll.MAX)
