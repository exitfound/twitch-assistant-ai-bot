"""Shared pieces of game messages: reward titles, the two titles' notes and curse notes.

Needed by the !roll reply, by reward outcomes and by reward descriptions on Twitch.
"""
import re

from src.core.config import Rewards, Roll
from src.core.content import Content
from src.twitch.local.roll import rules
from src.twitch.local.roll.game import Outcome


# How many characters of a viewer's input to quote in a refusal: people write anything there
INPUT_PREVIEW_CHARS = 25


def input_preview(raw: str) -> str:
    return raw.strip()[:INPUT_PREVIEW_CHARS]


def reward_title(action: str) -> str:
    return Content.text(f'reward_{action}_title')


# Words whose own period ends a sentence too: «ещё 5 мин.» keeps it before a note
ABBREVIATIONS = ('мин.', 'сек.')


# Twitch takes at most this many characters in one chat message and refuses a longer one
MESSAGE_MAX = 500

# An abbreviation's period is not a place to cut: «ещё 5 мин. – минус 20» stays one sentence
_SENTENCE_END = re.compile(r'(?<=[.!?…])(?<!мин\.)(?<!сек\.)\s+')


def split_message(text: str, limit: int | None = None) -> list[str]:
    """A message cut into pieces Twitch takes (MESSAGE_MAX), at sentence ends; a sentence longer
    than the limit on its own is cut at a space."""
    limit = limit or MESSAGE_MAX
    pieces: list[str] = []
    current = ''
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > limit:
            cut = sentence.rfind(' ', 0, limit)
            if cut <= 0:
                cut = limit
            head, sentence = sentence[:cut].rstrip(), sentence[cut:].lstrip()
            if current:
                pieces.append(current)
                current = ''
            pieces.append(head)
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f'{current} {sentence}' if current else sentence
    if current:
        pieces.append(current)
    return pieces


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


def luck_values(mode: str | None = None, stacks: int | None = None) -> dict:
    """Luck flask parameters for the texts at the strength of the moment (rules.luck_of()): where
    the lucky range starts, the step of the top's chance and the chance it grows to, in percent."""
    luck = rules.luck_of(mode, stacks or 1)
    return {
        'luck_from': luck.floor, 'luck_step': percent(luck.step),
        'luck_top': percent(100 * rules.luck_chance(Rewards.LUCK_THROWS, luck)),
    }


def percent(value: float) -> int | float:
    """A percentage for the texts: whole when it is whole, else to one decimal."""
    return int(value) if value == int(value) else round(value, 1)


def mass_values() -> dict:
    """The mass flasks' parameters for the texts: the range of АГОНИЯ's roll, its curse chance in
    percent and the cap of every throw while it holds, the bottom of БЛАГОСЛОВЕНИЕ's roll."""
    return {
        'agony_from': Roll.MIN, 'agony_top': rules.agony_top(), 'agony_curse': round(100 * Rewards.AGONY_CURSE),
        'agony_throw_top': rules.agony_throw_top(),
        'bless_from': rules.bless_floor(),
    }


def out_of(outcome: Outcome) -> tuple[int | None, int]:
    """The «из» (out of) of a throw for the texts: (the curse's ceiling, the top), each lowered to
    АГОНИЯ ЧЕРНОМИРЬЯ's cap when it held the throw."""
    cap = outcome.agony or Roll.MAX
    ceiling = None if outcome.ceiling is None else min(outcome.ceiling, cap)
    return ceiling, min(cap, Roll.MAX)


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


def sglaz_note(outcome: Outcome) -> str:
    """Note that the previous stream's Сглаз was laid on this throw. Empty otherwise.

    A reroll or an extra roll bought before writing in chat lays it with no «Сглаз на
    тебе» before: without the note the chat would see a cap and not know where it came from.
    """
    return Content.text('roll_sglaz_laid', target=outcome.target) if outcome.sglaz else ''


def loser_note(outcome: Outcome) -> str:
    """«Подзалупный китежанин стрима: …» for a move's message. Empty while one player is in the game."""
    if outcome.loser is None:
        return ''
    name, value = outcome.loser
    return Content.text('roll_loser_note', loser=name, loser_val=value, max=Roll.MAX)


def champion_note(outcome: Outcome) -> str:
    """«Китежанин стрима: …» for the throw message. Empty if there is nobody to show."""
    if outcome.champion is None:
        return ''
    name, value = outcome.champion
    return Content.text('roll_champion', champion=name, champion_val=value, max=Roll.MAX)
