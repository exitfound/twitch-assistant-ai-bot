import logging
import random
import re

logger = logging.getLogger(__name__)

MENTION_RE = re.compile(r'@\S+')

# Addressing the bot by word: «сосур» in Cyrillic and «secur» in Latin, one entry per
# variant. It lives here rather than in the dispatcher because src/core/database.py
# uses the same pattern to mark addressings in stored chat messages.
SOSUR_VARIANTS = ('сосур', 'secur')
SOSUR_RE = re.compile(
    r'(?:{})\w*'.format('|'.join(SOSUR_VARIANTS)), re.IGNORECASE | re.UNICODE
)

# Twitch message limits
TWITCH_MSG_MAX = 450
TWITCH_CHUNK_MAX = 450
TWITCH_TOTAL_MAX = TWITCH_CHUNK_MAX * 3
CHUNK_SEND_DELAY = 1.5

# Character limits for specific commands
WHO_MAX = 420

MIN_CAPS_LETTERS = 3
CAPS_THRESHOLD = 0.85


def random_delay(min_minutes: float, max_minutes: float) -> float:
    """A random pause in seconds between min and max minutes.

    A fixed interval in chat reads as a schedule: viewers notice the bot posts
    every N minutes. The spread makes it feel more alive.
    """
    return random.uniform(min(min_minutes, max_minutes), max(min_minutes, max_minutes)) * 60


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


def split_into_chunks(text: str, max_chunk: int = TWITCH_CHUNK_MAX,
                      max_total: int = TWITCH_TOTAL_MAX) -> list[str]:
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

# What to strip around a nick in a command argument: «!who @ник,» is the nick «ник»
NICK_TRAILING = ',.:;!?'
# A Twitch login is at most 25 characters: anything longer is not a nick
NICK_MAX = 25


def clean_nick(raw: str) -> str:
    """A nick from a command argument: no @, no trailing punctuation, lowercased.

    Cut to NICK_MAX because the answer echoes it back («@ник, @цель ни разу не писал»):
    a Twitch login is at most 25 characters, so nothing real is lost, and an argument of
    arbitrary length cannot be turned into a message of the viewer's choosing.
    """
    return raw.lstrip('@').rstrip(NICK_TRAILING).lower()[:NICK_MAX]


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


def defuse(text: str) -> str:
    """Make a line safe to send on its own, with no nick in front of it.

    A message starting with `/` or `.` reads as a chat command. The Helix endpoint the
    bot sends through does not execute them, so this is about how it looks in chat,
    not about privileges.
    """
    return text.lstrip('/.').lstrip()


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


def safe_format(template: str, **values) -> str:
    """Fill a template from the file. A broken template does not crash the handler."""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        logger.warning('Не удалось подставить значения в шаблон: %r', template[:80])
        return template


def find_banned(text: str, banned: list[str]) -> str | None:
    """The first stop-list word found in the text, or None."""
    if not banned:
        return None
    lowered = text.lower()
    for word in banned:
        if word.lower() in lowered:
            return word
    return None


def reply_to_bot(message, bot_id) -> str | None:
    """The bot's line this chat message replies to, or None if it is not a reply to the bot.

    twitchio's ChatMessageReply carries parent_user (a PartialUser), not
    parent_user_id: reading the latter yields '' and no reply is ever recognised,
    which only stays invisible because Twitch puts @botname at the start of a reply.
    """
    reply = getattr(message, 'reply', None)
    parent = getattr(reply, 'parent_user', None)
    if parent is None or str(getattr(parent, 'id', '')) != str(bot_id):
        return None
    return getattr(reply, 'parent_message_body', None) or None
