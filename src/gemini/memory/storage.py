"""Queries on the memory tables: chronicles, chatter_events, chatter_profiles.

The memory works on conversations (Block), not on bot sessions: chat split by
silence, keyed by the Moscow time of its first message.

The schema lives in init_db() (src/core/database.py) with all the others. Chat rows
are read with the same «not a command» filter as everywhere else, which covers the
rows that still hold commands.
"""
import bisect
import json
import random
import re
import time
from enum import StrEnum
from typing import NamedTuple

from src.core.database import get_db, invalidate_knowledge_cache
from src.core.utils import local_time

class ChronicleStatus(StrEnum):
    OK = 'ok'
    FAILED = 'failed'
    # Too few messages for a chronicle: the row only marks them as seen
    SKIPPED = 'skipped'

_NOT_COMMAND = "message NOT LIKE '!%'"


def _like(text: str) -> str:
    """A LIKE pattern matching text literally: nicks contain `_`, a LIKE wildcard."""
    escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


class Profile(NamedTuple):
    username: str
    portrait: str
    relations: list[dict]
    last_conversation: str
    sessions_seen: int


class Block(NamedTuple):
    """One conversation: chat between two silences of Memory.SILENCE_MINUTES.

    The chat on this channel happens only on streams, so a conversation is in
    practice a stream – but told by the messages alone, not by the stream state:
    no Twitch event, bot restart or missed stream end can shift it.
    """
    key: str          # time of its first message in BOT_TIMEZONE, 'YYYY-MM-DD HH:MM'
    first_id: int     # chat_messages ids, inclusive
    last_id: int
    count: int        # messages, commands not counted

    @property
    def span(self) -> tuple[int, int]:
        return self.first_id, self.last_id


def _key(ts: float) -> str:
    # Same shape and zone as a stream session: readable, sorts by time, and alike in the
    # container and in a --build-memory run by hand – the crash-rerun check in
    # update_profile() compares these keys
    return local_time(ts).strftime('%Y-%m-%d %H:%M')


async def chat_blocks(silence_minutes: int, *, uncovered: bool) -> list[Block]:
    """Finished conversations, oldest first.

    uncovered – only messages no chronicle row covers yet: what the memory has
    not seen. The last conversation counts as finished only once the chat has
    been quiet for silence_minutes; until then it may still go on.
    """
    db = await get_db()
    cover = (
        ' AND NOT EXISTS (SELECT 1 FROM chronicles c'
        ' WHERE m.id BETWEEN c.first_id AND c.last_id)'
    ) if uncovered else ''
    async with db.execute(
        f"SELECT m.id, CAST(strftime('%s', m.created_at) AS INTEGER) FROM chat_messages m"
        f' WHERE {_NOT_COMMAND}{cover} ORDER BY m.id'
    ) as cursor:
        rows = await cursor.fetchall()
    silence = silence_minutes * 60
    groups: list[list[tuple[int, int]]] = []
    for row in rows:
        if not groups or row[1] - groups[-1][-1][1] >= silence:
            groups.append([])
        groups[-1].append(row)
    if groups and time.time() - groups[-1][-1][1] < silence:
        groups.pop()
    # A covered conversation shares its key with its own chronicle – that is no clash
    taken = await _chronicle_keys() if uncovered else set()
    blocks = []
    for group in groups:
        key = _key(group[0][1])
        if key in taken:
            # Only when a new conversation starts in the same minute as an older chronicle
            key = f'{key} #{group[0][0]}'
        blocks.append(Block(key, group[0][0], group[-1][0], len(group)))
    return blocks


async def _chronicle_keys() -> set[str]:
    db = await get_db()
    async with db.execute('SELECT conversation FROM chronicles') as cursor:
        return {row[0] for row in await cursor.fetchall()}


async def memory_built() -> bool:
    """Whether bot.py --build-memory has finished the history (memory_state 'built')."""
    db = await get_db()
    async with db.execute("SELECT 1 FROM memory_state WHERE key = 'built'") as cursor:
        return await cursor.fetchone() is not None


async def mark_built() -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO memory_state (key, value) VALUES ('built', datetime('now'))"
    )
    await db.commit()


async def failed_blocks() -> list[Block]:
    """Conversations whose chronicle Gemini did not write – for the CLI to retry."""
    db = await get_db()
    async with db.execute(
        'SELECT conversation, first_id, last_id, message_count FROM chronicles'
        ' WHERE status = ? ORDER BY first_id',
        (ChronicleStatus.FAILED,),
    ) as cursor:
        return [Block(*row) for row in await cursor.fetchall()]


def _span(span: tuple[int, int] | None) -> tuple[str, tuple]:
    return (' AND id BETWEEN ? AND ?', span) if span else ('', ())


async def block_chat(block: Block) -> list[tuple[str, str]]:
    db = await get_db()
    async with db.execute(
        f'SELECT username, message FROM chat_messages WHERE id BETWEEN ? AND ? AND {_NOT_COMMAND}'
        ' ORDER BY id',
        block.span,
    ) as cursor:
        return list(await cursor.fetchall())


async def block_session(block: Block) -> str | None:
    """The bot session most of the conversation was in – for the game results."""
    db = await get_db()
    async with db.execute(
        'SELECT session_id FROM chat_messages WHERE id BETWEEN ? AND ?'
        ' GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1',
        block.span,
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def active_chatters(block: Block, min_messages: int) -> list[str]:
    """Who wrote at least min_messages in the conversation, most active first."""
    db = await get_db()
    async with db.execute(
        f'SELECT username FROM chat_messages WHERE id BETWEEN ? AND ? AND {_NOT_COMMAND}'
        ' GROUP BY username HAVING COUNT(*) >= ? ORDER BY COUNT(*) DESC',
        (*block.span, min_messages),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def ever_active_chatters(blocks: list[Block], min_messages: int) -> dict[str, Block]:
    """Who reached min_messages in at least one of the conversations, most active first.

    Maps each to the last conversation they wrote in: their first profile is
    dated by it.
    """
    if not blocks:
        return {}
    db = await get_db()
    async with db.execute(
        f'SELECT id, username FROM chat_messages WHERE id BETWEEN ? AND ? AND {_NOT_COMMAND}'
        ' ORDER BY id',
        (blocks[0].first_id, blocks[-1].last_id),
    ) as cursor:
        rows = await cursor.fetchall()
    starts = [b.first_id for b in blocks]
    counts: dict[tuple[str, int], int] = {}
    for row_id, username in rows:
        i = bisect.bisect_right(starts, row_id) - 1
        if i >= 0 and row_id <= blocks[i].last_id:
            counts[username, i] = counts.get((username, i), 0) + 1
    total: dict[str, int] = {}
    last: dict[str, Block] = {}
    for (username, _), n in counts.items():
        if n >= min_messages:
            total[username] = total.get(username, 0) + n
    for (username, i), _ in sorted(counts.items(), key=lambda item: item[0][1]):
        if username in total:
            last[username] = blocks[i]
    return {u: last[u] for u in sorted(total, key=total.get, reverse=True)}


async def user_messages(username: str, limit: int, span: tuple[int, int] | None = None) -> list[str]:
    """The chatter's messages, oldest first: the last `limit`, in one conversation or all time."""
    db = await get_db()
    where, params = _span(span)
    async with db.execute(
        f'SELECT message FROM chat_messages WHERE username = ?{where} AND {_NOT_COMMAND}'
        ' ORDER BY id DESC LIMIT ?',
        (username, *params, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in reversed(rows)]


async def mentions_of(username: str, limit: int,
                      span: tuple[int, int] | None = None) -> list[tuple[str, str]]:
    """Other chatters' messages that name the chatter, oldest first.

    A nick is ASCII, so LIKE (case-insensitive for ASCII) finds both @nick and a bare nick.
    """
    db = await get_db()
    where, params = _span(span)
    async with db.execute(
        f"SELECT username, message FROM chat_messages WHERE username != ? AND message LIKE ? ESCAPE '\\'"
        f'{where} AND {_NOT_COMMAND} ORDER BY id DESC LIMIT ?',
        (username, _like(username), *params, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))


async def known_nicks(nicks: list[str]) -> set[str]:
    """Which of the nicks ever wrote in chat."""
    if not nicks:
        return set()
    db = await get_db()
    marks = ','.join('?' * len(nicks))
    async with db.execute(
        f'SELECT DISTINCT username FROM chat_messages WHERE username IN ({marks})', nicks,
    ) as cursor:
        return {row[0] for row in await cursor.fetchall()}


async def sessions_seen(username: str, up_to_id: int) -> int:
    """In how many bot sessions (streams, or days before streams were tracked) the
    chatter wrote anything, up to the message with this id."""
    db = await get_db()
    async with db.execute(
        f'SELECT COUNT(DISTINCT session_id) FROM chat_messages'
        f' WHERE username = ? AND id <= ? AND {_NOT_COMMAND}',
        (username, up_to_id),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def save_chronicle(block: Block, text: str, status: str,
                         events: list[tuple[str, str]]) -> None:
    """The chronicle and its events in one go. A repeat replaces both.

    The row covers the conversation's messages: the memory will not take them again.
    """
    db = await get_db()
    await db.execute('DELETE FROM chatter_events WHERE conversation = ?', (block.key,))
    await db.executemany(
        'INSERT INTO chatter_events (conversation, username, event) VALUES (?, ?, ?)',
        [(block.key, nick, event) for nick, event in events],
    )
    await db.execute(
        'INSERT OR REPLACE INTO chronicles'
        ' (conversation, text, status, message_count, first_id, last_id) VALUES (?, ?, ?, ?, ?, ?)',
        (block.key, text, status, block.count, block.first_id, block.last_id),
    )
    await db.commit()


async def user_events(username: str, limit: int, before: str | None = None) -> list[tuple[str, str]]:
    """(conversation, event) of the chatter, oldest first: the last `limit`."""
    db = await get_db()
    where = 'username = ?' + (' AND conversation <= ?' if before else '')
    params = (username, before) if before else (username,)
    async with db.execute(
        f'SELECT conversation, event FROM chatter_events WHERE {where} ORDER BY conversation DESC, id DESC LIMIT ?',
        (*params, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))


async def random_events(username: str, limit: int) -> list[str]:
    """Random events of the chatter over the whole history. The table is small
    (thousands of rows), so ORDER BY RANDOM() over one chatter's rows is cheap."""
    db = await get_db()
    async with db.execute(
        'SELECT event FROM chatter_events WHERE username = ? ORDER BY RANDOM() LIMIT ?',
        (username, limit),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def random_messages(username: str, limit: int, min_chars: int) -> list[str]:
    """Random messages of the chatter spread over their whole history: one from
    each bot session in turn, sessions in random order, so a single busy stream
    cannot fill the sample. Only messages of at least min_chars – «ахах» and «+»
    say nothing about a person."""
    db = await get_db()
    async with db.execute(
        f'SELECT id, session_id FROM chat_messages'
        f' WHERE username = ? AND length(message) >= ? AND {_NOT_COMMAND}',
        (username, min_chars),
    ) as cursor:
        rows = await cursor.fetchall()
    by_session: dict[str, list[int]] = {}
    for message_id, session in rows:
        by_session.setdefault(session, []).append(message_id)
    pools = list(by_session.values())
    for pool in pools:
        random.shuffle(pool)
    random.shuffle(pools)
    picked: list[int] = []
    while pools and len(picked) < limit:
        for pool in pools:
            if len(picked) >= limit:
                break
            picked.append(pool.pop())
        pools = [pool for pool in pools if pool]
    if not picked:
        return []
    marks = ','.join('?' * len(picked))
    async with db.execute(
        f'SELECT id, message FROM chat_messages WHERE id IN ({marks})', picked,
    ) as cursor:
        text = dict(await cursor.fetchall())
    # In the order picked: random, not by time
    return [text[i] for i in picked]


async def chronicle_before(session_id: str | None) -> str | None:
    """The latest chronicle that ends before this bot session starts; with None –
    the latest of all. Failed and skipped rows have no text."""
    db = await get_db()
    where, params = ('', ())
    if session_id is not None:
        where = ' AND last_id < COALESCE((SELECT MIN(id) FROM chat_messages WHERE session_id = ?), 1e18)'
        params = (session_id,)
    async with db.execute(
        f"SELECT text FROM chronicles WHERE status = 'ok'{where} ORDER BY last_id DESC LIMIT 1",
        params,
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def session_chronicle(session_id: str) -> str | None:
    """The chronicle of the conversation that holds most of this bot session's chat.
    None until the memory has written it – that happens MEMORY_SILENCE_MINUTES
    after the chat goes quiet, so not right after a stream."""
    db = await get_db()
    async with db.execute(
        f"SELECT c.text FROM chronicles c JOIN chat_messages m ON m.id BETWEEN c.first_id AND c.last_id"
        f" WHERE m.session_id = ? AND c.status = 'ok' AND {_NOT_COMMAND}"
        ' GROUP BY c.conversation ORDER BY COUNT(*) DESC LIMIT 1',
        (session_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def get_profile(username: str) -> Profile | None:
    db = await get_db()
    async with db.execute(
        'SELECT username, portrait, relations, last_conversation, sessions_seen'
        ' FROM chatter_profiles WHERE username = ?',
        (username,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return Profile(row[0], row[1], _relations(row[2]), row[3], row[4])


def _relations(raw: str | None) -> list[dict]:
    """The stored relations, only well-formed {nick, note} entries: readers index them
    directly, and one bad entry would break every answer that mentions the chatter."""
    try:
        items = json.loads(raw or '[]')
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    return [r for r in items
            if isinstance(r, dict) and isinstance(r.get('nick'), str) and isinstance(r.get('note'), str)]


async def save_profile(profile: Profile) -> None:
    db = await get_db()
    await db.execute(
        'INSERT OR REPLACE INTO chatter_profiles'
        ' (username, portrait, relations, last_conversation, sessions_seen, updated_at)'
        ' VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
        (profile.username, profile.portrait, json.dumps(profile.relations, ensure_ascii=False),
         profile.last_conversation, profile.sessions_seen),
    )
    await db.commit()


MEMORY_TABLES = ('chronicles', 'chatter_events', 'chatter_profiles', 'memory_state')


async def clear_memory() -> None:
    db = await get_db()
    for table in MEMORY_TABLES:
        await db.execute(f'DELETE FROM {table}')
    await db.commit()


async def memory_counts() -> dict[str, int]:
    """Rows per memory table: what clear_memory() would delete."""
    db = await get_db()
    counts = {}
    for table in MEMORY_TABLES:
        async with db.execute(f'SELECT COUNT(*) FROM {table}') as cursor:
            counts[table] = (await cursor.fetchone())[0]
    return counts


# --- facts (the !fact table, retired) ------------------------------------

async def facts_naming(username: str) -> list[tuple[str, str]]:
    """(author, fact) of the saved facts that name the chatter by @nick.

    facts.username is the author, so a fact about someone is found by the mention. LIKE
    narrows the rows, the regex ends the nick at a word boundary: @nick2 is not @nick.
    """
    mention = re.compile('@' + re.escape(username) + r'(?!\w)', re.IGNORECASE)
    db = await get_db()
    async with db.execute(
        "SELECT username, fact FROM facts WHERE fact LIKE ? ESCAPE '\\' ORDER BY id", (_like(f'@{username}'),),
    ) as cursor:
        return [(author, fact) for author, fact in await cursor.fetchall() if mention.search(fact)]


async def facts_about(username: str) -> list[str]:
    """Saved facts that name the chatter by @nick – input for their first profile."""
    return [fact for _, fact in await facts_naming(username)]


async def move_unaddressed_facts() -> int:
    """Copy facts that name nobody by @nick into knowledge. Returns how many were new there.

    Facts that name a chatter by @nick belong in that chatter's profile instead, so
    only the rest is copied. The facts table itself is left in place.
    """
    db = await get_db()
    async with db.execute("SELECT fact FROM facts WHERE fact NOT LIKE '%@%' ORDER BY id") as cursor:
        facts = [row[0] for row in await cursor.fetchall()]
    added = 0
    for fact in facts:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO knowledge (content, source) VALUES (?, 'facts')", (fact,),
        )
        added += cursor.rowcount
    await db.commit()
    if added:
        invalidate_knowledge_cache()
    return added
