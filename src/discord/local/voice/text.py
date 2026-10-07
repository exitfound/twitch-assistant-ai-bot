"""A chat answer turned into text the Russian TTS model reads well.

The server's own normalization is English (@ → «at», digits → English numbers) and is
what gave the voice an American accent, so it is off and the text is prepared here:
nicks and other Latin words in Cyrillic, emotes and links out, CAPS to normal case,
numbers to Russian words.
"""
import re

from src.core.content import Content
from src.gemini.output import trim_to_sentence

# Latin digraphs first: «sh» must become «ш» before «s» and «h» are read one by one
LEET = str.maketrans({'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's'})
DIGRAPHS = [('sch', 'ш'), ('tch', 'ч'), ('sh', 'ш'), ('ch', 'ч'), ('zh', 'ж'), ('kh', 'х'), ('th', 'т'), ('ph', 'ф'),
            ('ck', 'к'), ('qu', 'кв'), ('ee', 'и'), ('oo', 'у'), ('ou', 'ау'), ('ow', 'оу'), ('ya', 'я'), ('yu', 'ю'),
            ('yo', 'йо'), ('ye', 'е'), ('ai', 'ей'), ('ay', 'ей'), ('ea', 'и'), ('x', 'кс'), ('j', 'дж')]
LETTERS = dict(zip('abcdefghiklmnopqrstuvwyz', 'абкдефгхиклмнопкрстуввыз', strict=True))

ONES = ['ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять', 'десять', 'одиннадцать',
        'двенадцать', 'тринадцать', 'четырнадцать', 'пятнадцать', 'шестнадцать', 'семнадцать', 'восемнадцать',
        'девятнадцать']
TENS = ['', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто']
HUNDREDS = ['', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот', 'семьсот', 'восемьсот', 'девятьсот']

LINK_RE = re.compile(r'https?://\S+|www\.\S+')
EMOJI_RE = re.compile(r'[\U0001F000-\U0001FAFF☀-➿️]')
LATIN_RE = re.compile(r'@?[A-Za-z][A-Za-z0-9_]*')


def _under_1000(n: int, female: bool = False) -> list[str]:
    words = [HUNDREDS[n // 100]] if n >= 100 else []
    n %= 100
    if n >= 20:
        words.append(TENS[n // 10])
        n %= 10
    if n or not words:
        word = ONES[n]
        if female and n in (1, 2):
            word = 'одна' if n == 1 else 'две'
        words.append(word)
    return [w for w in words if w and not (w == 'ноль' and len(words) > 1)]


def number_words(n: int) -> str:
    """A number in Russian words, nominative case; a million and more digit by digit."""
    if n >= 1_000_000:
        return ' '.join(ONES[int(d)] for d in str(n))
    thousands, rest = divmod(n, 1000)
    words = []
    if thousands:
        last = thousands % 100
        if 10 < last < 20 or thousands % 10 in (0, 5, 6, 7, 8, 9):
            form = 'тысяч'
        else:
            form = 'тысяча' if thousands % 10 == 1 else 'тысячи'
        words += ([] if thousands == 1 else _under_1000(thousands, female=True)) + [form]
    if rest or not thousands:
        words += _under_1000(rest)
    return ' '.join(words)


def translit(word: str) -> str:
    """A Latin word as a Russian reader would say it, roughly."""
    w = word.lower().translate(LEET).strip('_')
    w = re.sub(r'y$', 'i', re.sub(r'(?<=[^aeiouy])y(?=[^aeiouy])', 'i', w))
    for latin, cyrillic in DIGRAPHS:
        w = w.replace(latin, cyrillic)
    return re.sub(r'[a-z_]', '', ''.join(LETTERS.get(c, c) for c in w))


def parse_nicks(lines: list[str]) -> dict[str, str]:
    """`nick = Как сказать` lines from CONTENT.md → {nick without @, lowercased: words}."""
    nicks = {}
    for line in lines:
        nick, sep, spoken = line.partition('=')
        if sep and nick.strip() and spoken.strip():
            nicks[nick.strip().lstrip('@').lower()] = spoken.strip()
    return nicks


def _decaps(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        # The whole message shouted: read in a normal voice
        text = text.lower()
    else:
        text = re.sub(r'\b[А-ЯЁ]{3,}\b', lambda m: m.group(0).lower(), text)
    return re.sub(r'(^|[.!?…]\s+)([а-яё])', lambda m: m.group(1) + m.group(2).upper(), text)


def prepare(text: str, nicks: dict[str, str], emotes: set[str], max_chars: int) -> str:
    """The text to synthesize, or '' when nothing speakable is left."""
    text = LINK_RE.sub('ссылка', text)
    # Emotes are matched case-sensitively, as Twitch does: «KEKW» is one, «kekw» a word
    text = ' '.join(word for word in text.split() if word not in emotes)
    text = EMOJI_RE.sub('', text)
    text = _decaps(text)
    # «@nick text» → «nick, text»: a pause after the name, as in speech
    text = re.sub(r'^(@[A-Za-z0-9_]+)\s+(?![,.!?])', r'\1, ', text)

    def latin(m: re.Match) -> str:
        word = m.group(0)
        known = nicks.get(word.lstrip('@').lower())
        if known:
            return known
        core = re.sub(r'\d+$', '', word.lstrip('@'))
        spoken = translit(core)
        return spoken.capitalize() if word[0].isupper() or word.startswith('@') else spoken

    text = LATIN_RE.sub(latin, text)
    text = re.sub(r'\d+', lambda m: number_words(int(m.group(0))), text)
    text = text.replace('@', '').replace('_', ' ').replace('«', '"').replace('»', '"')
    text = re.sub(r'([!?.])\1+', r'\1', text)
    text = re.sub(r'\s{2,}', ' ', text).strip(' ,')
    if not re.search(r'[а-яёА-ЯЁ]', text):
        return ''
    text = trim_to_sentence(text, max_chars)
    return text[:1].upper() + text[1:]


def spoken(text: str, max_chars: int) -> str:
    """prepare() with the nick dictionary and the emotes from CONTENT.md."""
    return prepare(text, parse_nicks(Content.items('voice_nicks')), set(Content.items('emotes')), max_chars)
