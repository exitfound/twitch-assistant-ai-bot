"""Shared pieces of game messages: reward titles, champion (китежанин) and curse notes.

Needed by the !roll reply, by reward outcomes and by reward descriptions on Twitch.
"""
from src.core.config import Rewards, Roll
from src.core.content import Content
from src.twitch.local.roll.game import Outcome


# How many characters of a viewer's input to quote in a refusal: people write anything there
INPUT_PREVIEW_CHARS = 25


def input_preview(raw: str) -> str:
    return raw.strip()[:INPUT_PREVIEW_CHARS]


def reward_title(action: str) -> str:
    return Content.text(f'reward_{action}_title')


# Words whose own period ends a sentence too: «ещё 5 мин.» keeps it before a note
ABBREVIATIONS = ('мин.', 'сек.')


def compose(*parts: str) -> str:
    """Join message pieces with spaces. A note in brackets belongs to the sentence before
    it, so that sentence's closing marks move past the note: «… (95 из 100) (осталось 2).»"""
    text = ''
    for part in filter(None, parts):
        body = text.rstrip('.!?…')
        marks = text[len(body):]
        if not (text and part.startswith('(') and marks):
            text = f'{text} {part}' if text else part
        elif text.endswith(ABBREVIATIONS):
            text = f'{text} {part}.'
        else:
            text = f'{body} {part}{marks}'
    return text


def curse_values() -> dict:
    """Curse parameters for the texts: in the reward description and in chat."""
    return {
        'step': Rewards.CURSE_STEP, 'floor': Rewards.CURSE_FLOOR,
        'hold': Rewards.CURSE_HOLD_MINUTES,
    }


def curse_note(outcome: Outcome) -> str:
    """Note appended to the message about a cursed player's throw. Empty if there is no curse.

    Any throw on a cursed player lowers the ceiling, so the note is always about the
    next ceiling – or about the minutes until the lift if the ceiling is already on the floor.
    """
    if outcome.ceiling is None:
        return ''
    key = 'roll_curse_hold' if outcome.curse_minutes_left is not None else 'roll_curse_step'
    return Content.text(
        key, ceiling=outcome.ceiling, next=outcome.next_ceiling,
        minutes=outcome.curse_minutes_left, **curse_values(),
    )


def champion_note(outcome: Outcome) -> str:
    """«Китежанин стрима: …» for the throw message. Empty if there is nobody to show."""
    if outcome.champion is None:
        return ''
    name, value = outcome.champion
    return Content.text('roll_champion', champion=name, champion_val=value, max=Roll.MAX)
