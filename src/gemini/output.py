"""What a Gemini answer goes through before it reaches chat.

Twitch limits, CAPS, markdown, dashes, links and pings, trimming at a sentence end and
splitting into messages. Only the Gemini layer produces free text, so only it needs
these; what every package uses (nicks, templates, defuse) stays in src/core/utils.py.
"""
import re

# Twitch message limit
TWITCH_MSG_MAX = 450
CHUNK_SEND_DELAY = 1.5

# Character limits for specific commands
WHO_MAX = 420

MIN_CAPS_LETTERS = 3
CAPS_THRESHOLD = 0.85

MENTION_RE = re.compile(r'@\S+')


def is_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < MIN_CAPS_LETTERS:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= CAPS_THRESHOLD


def caps_preserve_mentions(text: str) -> str:
    parts = MENTION_RE.split(text)
    mentions = MENTION_RE.findall(text)
    result = []
    for i, part in enumerate(parts):
        result.append(part.upper())
        if i < len(mentions):
            result.append(mentions[i])
    return ''.join(result)


def strip_markdown(text: str) -> str:
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+\s*', '', text)
    # No `_` here: it is part of nicks (m1ndsh1ft_, limbo_______), and stripping it
    # makes the bot mention people who do not exist
    text = re.sub(r'[`~>|]', '', text)
    text = re.sub(r'^\s*[-\u2022\u25cf]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[\.\)]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def split_into_chunks(text: str, max_chunk: int, max_total: int) -> list[str]:
    if len(text) > max_total:
        text = text[:max_total - 3] + '...'
    chunks = []
    while text:
        if len(text) <= max_chunk:
            chunks.append(text)
            break
        cut = text.rfind(' ', 0, max_chunk)
        if cut <= 0:
            cut = max_chunk
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks


# The em dash is kept out of chat: the whole project uses only the en dash (U+2013).
# The model emits em dashes on its own, so they are replaced on output
EM_DASH = '\u2014'
EN_DASH = '\u2013'

_URL_RE = re.compile(r'(?:(?:https?|ftp)://|www\.)\S+|\b\S+\.(?:com|net|org|ru|io|me|tv|gg|xyz|link)(?:/\S*)?',
                     re.IGNORECASE)


def strip_links(text: str) -> str:
    """Remove anything that reads as a link.

    For messages nobody is watching. The chat is part of the model's context, so a
    viewer can plant text for the bot to repeat later, and a repeated link is what
    turns that into harm for a third person.
    """
    return re.sub(r'\s{2,}', ' ', _URL_RE.sub('', text)).strip()


def strip_pings(text: str) -> str:
    """Drop the `@` from mentions, keeping the word itself.

    The name still reads, but the bot cannot be made to ping someone on demand.
    """
    return re.sub(r'@(\w)', r'\1', text)


def fix_dashes(text: str) -> str:
    return text.replace(EM_DASH, EN_DASH)


_SENTENCE_END = re.compile(r'[.!?…](?=\s|$)')


def trim_to_sentence(text: str, limit: int) -> str:
    """Trim text to limit characters at a sentence end.

    An answer cut off mid-word reads worse than one a sentence shorter. If there
    is no sentence end within the limit (or it is in the first half), cut at a
    word and add an ellipsis.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    # A mark ends a sentence only before a space or the end of the text: the dot
    # inside «23.06» or «v1.2» does not
    ends = [m.start() for m in _SENTENCE_END.finditer(text, 0, limit + 1) if m.start() < limit]
    end = ends[-1] if ends else -1
    if end >= limit // 2:
        return head[:end + 1]
    cut = head.rfind(' ', 0, limit - 1)
    return head[:cut if cut > 0 else limit - 1].rstrip(' ,;:–-') + '…'


def cleanup_response(text: str, user: str, max_len: int = TWITCH_MSG_MAX) -> str:
    text = fix_dashes(text)
    # Asterisks (*ФАКТ*) and backticks (`terraform`, which the model emits when it
    # quotes a command) would show as is, and chat has no line breaks
    text = re.sub(r'[*`]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    mention = re.compile('@' + re.escape(user) + r'(?!\w)', re.IGNORECASE)
    leading = mention.match(text)
    if leading:
        # The reply already starts with the asker's nick, so the model's own one goes,
        # and the separator after it with it: «@ник: текст», «@ник – текст».
        # Only a standalone dash, «@ник -5» stays «-5»
        text = re.sub(r'^[\s:,]*(?:[-' + EN_DASH + r']\s+)?', '', text[leading.end():])
    # Further mentions of the asker keep the nick, just without the ping: when the
    # asker is also the subject (!who on oneself, !versus with oneself) the nick is
    # the meaning, and deleting it leaves «Победитель – .»
    text = mention.sub(user, text).strip()
    # The model exceeds the limit: cut at a sentence end, not mid-word
    return trim_to_sentence(text, max_len)


def find_banned(text: str, banned: list[str]) -> str | None:
    """The first stop-list word found in the text, or None."""
    if not banned:
        return None
    lowered = text.lower()
    for word in banned:
        if word.lower() in lowered:
            return word
    return None
