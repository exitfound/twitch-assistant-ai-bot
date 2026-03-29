import re

MENTION_RE = re.compile(r'@\S+')

# Twitch message limits
TWITCH_MSG_MAX = 450
TWITCH_CHUNK_MAX = 450
TWITCH_TOTAL_MAX = TWITCH_CHUNK_MAX * 3
CHUNK_SEND_DELAY = 1.5

# Character limits for specific commands
WHO_VERSUS_MAX = 420

MIN_CAPS_LETTERS = 3
CAPS_THRESHOLD = 0.85


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
    text = re.sub(r'[_`~>|]', '', text)
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


def cleanup_response(text: str, user: str, max_len: int = TWITCH_MSG_MAX) -> str:
    if text.lower().startswith(f'@{user}'):
        text = text[len(f'@{user}'):].lstrip(':,').strip()
    text = re.sub(re.escape(f'@{user}'), '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\s{2,}', ' ', text)
    if len(text) > max_len:
        text = text[:max_len - 3] + '...'
    return text
