"""The dossier of a chatter as one context line: a headline, a few facts, catchphrases.

Only a handful of facts go into one answer – a few on the question's topic, the freshest,
the rest at random (storage.pick_facts()) – so the bot uses a detail or two instead of
reciting the whole dossier, and every answer digs up something else.
"""
import asyncio
import random

from src.core.content import Content
from src.gemini.memory import storage

# Relations per person in one answer, picked at random: all of them is one more list
# for the model to recite
RELATIONS = 2


async def people(nicks: list[str], query: str, first: int, others: int, limit: int | None = None) -> list[str]:
    """One line per chatter that has a dossier, in the order given, repeats dropped, at most
    `limit` lines: a word that is not a chatter takes no place. The first nick gets `first`
    facts, the rest `others`."""
    nicks = list(dict.fromkeys(nicks))
    profiles, facts = await asyncio.gather(storage.get_profiles(nicks), storage.get_facts_of(nicks))
    lines = []
    for i, nick in enumerate(nicks):
        own = facts.get(nick, [])
        text = line(nick, profiles.get(nick), storage.pick_facts(own, first if i == 0 else others, query))
        if text:
            lines.append(text)
        if limit is not None and len(lines) == limit:
            break
    return lines


def catchphrases(phrases: tuple[str, ...]) -> str:
    """The catchphrases under their label, as the model sees them in every dossier."""
    return Content.label('catchphrases') + ': ' + ' / '.join(phrases)


def line(nick: str, profile: storage.Profile | None, facts: list[storage.Fact]) -> str:
    """The chatter's dossier as one line; '' – there is nothing in it to show."""
    parts = []
    if profile and profile.portrait:
        parts.append(profile.portrait)
    if facts:
        parts.append(Content.label('dossier_facts') + ': ' + '; '.join(f.text.rstrip('.') for f in facts) + '.')
    if profile and profile.catchphrases:
        parts.append(catchphrases(profile.catchphrases) + '.')
    relations = random.sample(profile.relations, min(RELATIONS, len(profile.relations))) if profile else []
    if relations:
        parts.append(Content.label('dossier_relations') + ': '
                     + '; '.join(f'{r["nick"]} – {r["note"]}' for r in relations) + '.')
    return f'{nick}: ' + ' '.join(parts) if parts else ''
