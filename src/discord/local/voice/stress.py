"""Ё and word stress for the TTS model, placed by ruaccent.

The model takes a stress as the stressed vowel in upper case («замОк»): «+» it reads
aloud as «плюс», and an acute accent garbles the word. ruaccent drops quotes and other
punctuation, so its marks are carried back onto the prepared text word by word.
"""
import logging
import re
import threading
from pathlib import Path

from src.discord.local.voice.text import CYRILLIC_WORD_RE, word_key

logger = logging.getLogger(__name__)

# The small homograph model without the full dictionary: 0.25 GB of memory and 20–40 ms
# an answer against 2 GB and 150 ms, with the same stresses on chat-like text
OMOGRAPH_MODEL = 'tiny2.1'
VOWELS = frozenset('аеёиоуыэюя')
MARKED_RE = re.compile(r'[а-яёА-ЯЁ+]+(?:-[а-яёА-ЯЁ+]+)*')
LOOKAHEAD = 4

_accentuator = None
_lock = threading.Lock()


def load(path: Path) -> None:
    """Load ruaccent once per process; its models are downloaded into path when missing."""
    global _accentuator
    with _lock:
        if _accentuator is None:
            import onnxruntime
            from ruaccent import RUAccent
            # onnxruntime reports usage to Microsoft unless told not to
            onnxruntime.disable_telemetry_events()
            accentuator = RUAccent()
            path.mkdir(parents=True, exist_ok=True)
            accentuator.load(omograph_model_size=OMOGRAPH_MODEL, use_dictionary=False, tiny_mode=True,
                             workdir=str(path))
            _accentuator = accentuator


def loaded() -> bool:
    return _accentuator is not None


def _spoken(marked: str, original: str) -> str:
    """A word with ruaccent's «+» before the stressed vowel → the original's case, ё and
    the stressed vowel in upper case. One-vowel words and ё carry no mark: there the
    stress is not in question."""
    plain = marked.replace('+', '')
    word = ''.join(c.upper() if o.isupper() else c for o, c in zip(original, plain, strict=True))
    at = marked.find('+')
    if at < 0 or at >= len(plain) or plain[at] == 'ё' or sum(c in VOWELS for c in plain.lower()) < 2:
        return word
    word = word.lower()
    return word[:at] + word[at].upper() + word[at + 1:]


def apply(text: str, marks: bool) -> str:
    """The text with ё restored and, if marks, the stresses marked; unchanged before load()
    or when ruaccent fails on it – the part is still spoken, only without the marks.

    A word with an upper-case letter after its first is marked already (lists.voice_words)
    and kept as it is.
    """
    if _accentuator is None:
        return text
    try:
        with _lock:
            found = MARKED_RE.findall(_accentuator.process_all(text))
    except Exception:
        logger.exception('Озвучка: ruaccent не разобрал фразу – она прозвучит без ударений и ё')
        return text
    at = 0

    def put(m: re.Match) -> str:
        nonlocal at
        word = m.group(0)
        if any(c.isupper() for c in word[1:]):
            return word
        # ruaccent may drop or split a token: look a few words ahead, and leave a word
        # without a match as it is rather than shift every word after it
        for i in range(at, min(at + LOOKAHEAD, len(found))):
            if word_key(found[i].replace('+', '')) == word_key(word):
                at = i + 1
                return _spoken(found[i] if marks else found[i].replace('+', ''), word)
        return word

    return CYRILLIC_WORD_RE.sub(put, text)
