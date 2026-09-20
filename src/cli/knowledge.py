"""Knowledge base import: lore files into the knowledge table, by source.

Three formats (--format):
  lines     – one entry per line, # starts a comment (the original format, default)
  telegram  – a Telegram Desktop chat export (result.json): every text message
              becomes «author: text»; service messages and media without a
              caption are skipped
  text      – an article or notes (.txt / .md): cut into pieces of 1–3 sentences,
              so a long text does not land in the random «language» sample as
              one wall of text

Every row remembers its source, so one source can be removed whole
(--clear-lore --source X) without touching the others; NULL means it is unknown.
"""
import json
import logging
import re
from pathlib import Path

from src.core.database import get_db, invalidate_knowledge_cache

logger = logging.getLogger(__name__)

FORMATS = ('lines', 'telegram', 'text')

# --format text: a piece is up to this many sentences, and stops growing
# once it reaches PIECE_CHARS – a long sentence still stays whole
PIECE_SENTENCES = 3
PIECE_CHARS = 300
_SENTENCE_END = re.compile(r'(?<=[.!?…])\s+(?=\S)')
_MD_LINK = re.compile(r'!?\[([^\]]*)\]\([^)]*\)')
# A numbered list item is 1–2 digits: a four-digit year with a dot starts a sentence,
# not list item number 2024
_MD_MARKUP = re.compile(r'^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d{1,2}[.)]\s+)')
_MD_EMPHASIS = re.compile(r'(\*\*|__|\*|`)')


class LoreError(Exception):
    """A file that cannot be read in the requested format."""


def parse_lore_file(path: str, fmt: str = 'lines') -> tuple[list[str], str]:
    """Entries of a lore file and its default source name."""
    if fmt == 'telegram':
        return _parse_telegram(path)
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    source = Path(path).name
    if fmt == 'text':
        return _parse_text(raw), source
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        entries.append(line)
    return entries, source


def _telegram_text(text) -> str:
    # text is a string, or a list of strings and {"type": ..., "text": ...} pieces
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return ''.join(part if isinstance(part, str) else str(part.get('text', '')) for part in text)
    return ''


def _parse_telegram(path: str) -> tuple[list[str], str]:
    """A Telegram Desktop export: one chat (result.json) or «all chats» (chats.list)."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise LoreError(f'{path}: не JSON экспорта Telegram ({e})') from e
    chats = data.get('chats', {}).get('list') if isinstance(data.get('chats'), dict) else None
    if chats is None:
        if 'messages' not in data:
            raise LoreError(f'{path}: нет messages – это не экспорт чата Telegram Desktop')
        chats = [data]
    entries = []
    for chat in chats:
        for message in chat.get('messages', []):
            if message.get('type') != 'message':
                continue
            # A long message is one entry anyway: splitting a chat line loses who said it
            text = ' '.join(_telegram_text(message.get('text')).split())
            if not text:
                continue
            author = message.get('from') or message.get('actor') or ''
            entries.append(f'{author}: {text}' if author else text)
    names = [c.get('name') for c in chats if c.get('name')]
    source = f'telegram:{names[0]}' if len(names) == 1 else f'telegram:{Path(path).name}'
    return entries, source


def _plain(line: str) -> str:
    """A markdown line as text: headings, quotes, list marks, links and emphasis removed."""
    line = _MD_LINK.sub(r'\1', line)
    line = _MD_MARKUP.sub('', line)
    return _MD_EMPHASIS.sub('', line).strip()


def _parse_text(raw: str) -> list[str]:
    """Paragraphs cut into pieces of up to PIECE_SENTENCES sentences."""
    pieces = []
    in_code = False
    paragraph: list[str] = []

    def flush() -> None:
        text = ' '.join(paragraph).strip()
        paragraph.clear()
        if not text:
            return
        current: list[str] = []
        for sentence in _SENTENCE_END.split(text):
            current.append(sentence)
            if len(current) >= PIECE_SENTENCES or len(' '.join(current)) >= PIECE_CHARS:
                pieces.append(' '.join(current))
                current = []
        if current:
            pieces.append(' '.join(current))

    for line in raw.splitlines():
        if line.strip().startswith('```'):
            # Code blocks say nothing about how the chat talks
            in_code = not in_code
            flush()
            continue
        if in_code:
            continue
        text = _plain(line)
        if not text:
            flush()
            continue
        paragraph.append(text)
        if _MD_MARKUP.match(line) and not line.lstrip().startswith('>'):
            # A heading or a list item is a unit of its own
            flush()
    flush()
    return pieces


async def count_knowledge(source: str | None = None) -> int:
    """How many rows clear_knowledge() would delete."""
    db = await get_db()
    sql, params = ('SELECT COUNT(*) FROM knowledge', ()) if source is None else (
        'SELECT COUNT(*) FROM knowledge WHERE source = ?', (source,))
    async with db.execute(sql, params) as cursor:
        return (await cursor.fetchone())[0]


async def clear_knowledge(source: str | None = None) -> int:
    """Delete the whole knowledge table, or only one source. Returns rows deleted.
    FTS is synced by a trigger."""
    db = await get_db()
    if source is None:
        cursor = await db.execute('DELETE FROM knowledge')
    else:
        cursor = await db.execute('DELETE FROM knowledge WHERE source = ?', (source,))
    await db.commit()
    invalidate_knowledge_cache()
    return cursor.rowcount


async def import_entries(entries: list[str], source: str | None = None) -> tuple[int, int]:
    """Import entries into knowledge (FTS is synced by a trigger). Returns (added, skipped).

    content is unique across sources: a line that is already there keeps its
    original source and counts as skipped.
    """
    db = await get_db()
    added = 0
    skipped = 0
    for entry in entries:
        cursor = await db.execute(
            'INSERT OR IGNORE INTO knowledge (content, source) VALUES (?, ?)', (entry, source)
        )
        if cursor.rowcount:
            added += 1
        else:
            skipped += 1
    await db.commit()
    invalidate_knowledge_cache()
    return added, skipped


async def lore_sources() -> list[tuple[str | None, int]]:
    """(source, rows), largest first; None – rows whose source is unknown."""
    db = await get_db()
    async with db.execute(
        'SELECT source, COUNT(*) FROM knowledge GROUP BY source ORDER BY COUNT(*) DESC'
    ) as cursor:
        return list(await cursor.fetchall())
