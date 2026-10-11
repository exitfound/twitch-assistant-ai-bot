"""Queries on the memory tables: chronicles, chatter_events, chatter_profiles, chatter_facts.

The memory works on conversations (Block), not on bot sessions: chat split by
silence, keyed by the Moscow time of its first message.

The schema lives in init_db() (src/core/db/schema.py) with all the others. Chat rows
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

from src.core.database import get_db, invalidate_knowledge_cache, transaction
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
    portrait: str               # the dossier's headline: who they are, in a line or two
    relations: list[dict]
    last_conversation: str
    sessions_seen: int
    catchphrases: tuple[str, ...] = ()


# The prompt asks for up to 250 characters; the model sometimes writes more. A longer
# portrait is a paragraph profile from before the dossiers and counts as no headline
HEADLINE_MAX_CHARS = 300


def has_headline(profile: Profile | None) -> bool:
    """A dossier headline, not an old paragraph profile that --build-memory rebuilds."""
    return profile is not None and 0 < len((profile.portrait or '').strip()) <= HEADLINE_MAX_CHARS


class Fact(NamedTuple):
    id: int
    kind: str
    text: str
    last_seen: str
    hits: int


# The kinds of a dossier fact, in the order the model is shown them
FACT_KINDS = ('who', 'interest', 'manner', 'stance', 'tease', 'event')


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
    # Chronicle ranges never overlap, so only the one starting last at or before a
    # message can cover it: one lookup in idx_chronicles_first instead of a scan
    # of every chronicle per message
    cover = (
        ' AND COALESCE((SELECT c.last_id FROM chronicles c WHERE c.first_id <= m.id'
        ' ORDER BY c.first_id DESC LIMIT 1), -1) < m.id'
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
    async with transaction() as db:
        await db.execute(
            "INSERT OR REPLACE INTO memory_state (key, value) VALUES ('built', datetime('now'))"
        )


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
    async with transaction() as db:
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


_PROFILE_COLUMNS = 'username, portrait, relations, last_conversation, sessions_seen, catchphrases'


def _profile(row: tuple) -> Profile:
    return Profile(row[0], row[1], _relations(row[2]), row[3], row[4], _phrases(row[5]))


async def get_profile(username: str) -> Profile | None:
    db = await get_db()
    async with db.execute(
        f'SELECT {_PROFILE_COLUMNS} FROM chatter_profiles WHERE username = ?', (username,),
    ) as cursor:
        row = await cursor.fetchone()
    return _profile(row) if row else None


async def get_profiles(usernames: list[str]) -> dict[str, Profile]:
    """The profiles of those of the usernames that have one, in one query."""
    if not usernames:
        return {}
    db = await get_db()
    marks = ', '.join('?' * len(usernames))
    async with db.execute(
        f'SELECT {_PROFILE_COLUMNS} FROM chatter_profiles WHERE username IN ({marks})', usernames,
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0]: _profile(row) for row in rows}


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


def _phrases(raw: str | None) -> tuple[str, ...]:
    try:
        items = json.loads(raw or '[]')
    except ValueError:
        return ()
    return tuple(p for p in items if isinstance(p, str)) if isinstance(items, list) else ()


async def save_profile(profile: Profile) -> None:
    async with transaction() as db:
        await db.execute(
            'INSERT OR REPLACE INTO chatter_profiles'
            ' (username, portrait, relations, last_conversation, sessions_seen, catchphrases, updated_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
            (profile.username, profile.portrait, json.dumps(profile.relations, ensure_ascii=False),
             profile.last_conversation, profile.sessions_seen,
             json.dumps(list(profile.catchphrases), ensure_ascii=False)),
        )


# --- dossier facts --------------------------------------------------------------

async def get_facts(username: str) -> list[Fact]:
    """The chatter's active facts, oldest first."""
    db = await get_db()
    async with db.execute(
        'SELECT id, kind, text, last_seen, hits FROM chatter_facts'
        ' WHERE username = ? AND active = 1 ORDER BY id',
        (username,),
    ) as cursor:
        return [Fact(*row) for row in await cursor.fetchall()]


async def get_facts_of(usernames: list[str]) -> dict[str, list[Fact]]:
    """Active facts of several chatters in one query, oldest first; no facts – no key."""
    if not usernames:
        return {}
    db = await get_db()
    marks = ', '.join('?' * len(usernames))
    async with db.execute(
        'SELECT username, id, kind, text, last_seen, hits FROM chatter_facts'
        f' WHERE username IN ({marks}) AND active = 1 ORDER BY id',
        usernames,
    ) as cursor:
        rows = await cursor.fetchall()
    facts: dict[str, list[Fact]] = {}
    for username, *fact in rows:
        facts.setdefault(username, []).append(Fact(*fact))
    return facts


async def users_with_dossier() -> set[str]:
    """Chatters whose dossier is built: a headline or an active fact. A quiet chatter's
    first dossier may hold no facts and is still done; an old paragraph profile is not."""
    db = await get_db()
    async with db.execute(
        'SELECT username FROM chatter_facts WHERE active = 1'
        " UNION SELECT username FROM chatter_profiles"
        " WHERE LENGTH(TRIM(COALESCE(portrait, ''))) BETWEEN 1 AND ?",
        (HEADLINE_MAX_CHARS,),
    ) as cursor:
        return {row[0] for row in await cursor.fetchall()}


async def apply_dossier(profile: Profile, new: list[tuple[str, str]], confirmed: list[int],
                        outdated: list[int], max_facts: int) -> None:
    """Merge one conversation into the dossier and save the profile, in one transaction.

    New facts are added, confirmed ones get a hit and the conversation as last_seen,
    outdated ones are switched off. A new fact worded as an active one counts as its
    confirmation: a conversation read twice must not double the dossier. Ids of another
    chatter's facts are ignored. Over max_facts the facts this conversation added or
    confirmed stay; of the rest the least confirmed go first, among equals the longest
    unconfirmed. A new fact has one hit, so ordering by hits alone would retire it at once.
    """
    conversation = profile.last_conversation
    async with transaction() as db:
        async with db.execute(
            'SELECT id, text FROM chatter_facts WHERE username = ? AND active = 1', (profile.username,),
        ) as cursor:
            rows = await cursor.fetchall()
        own = {fact_id for fact_id, _ in rows}
        # A repeat of a fact being retired is a new fact: confirming it would lose it
        known = {_same(text): fact_id for fact_id, text in rows if fact_id not in outdated}
        confirmed = set(confirmed)
        fresh = []
        for kind, text in new:
            key = _same(text)
            if key in known:
                confirmed.add(known[key])
            elif key not in {_same(t) for _, t in fresh}:
                fresh.append((kind, text))
        if hit := sorted(confirmed & own):
            await db.execute(
                'UPDATE chatter_facts SET hits = hits + 1, last_seen = ?'
                f' WHERE id IN ({", ".join("?" * len(hit))})',
                (conversation, *hit),
            )
        if gone := sorted(set(outdated) & own):
            await db.execute(
                f'UPDATE chatter_facts SET active = 0 WHERE id IN ({", ".join("?" * len(gone))})', gone,
            )
        await db.executemany(
            'INSERT INTO chatter_facts (username, kind, text, first_seen, last_seen)'
            ' VALUES (?, ?, ?, ?, ?)',
            [(profile.username, kind, text, conversation, conversation) for kind, text in fresh],
        )
        await db.execute(
            'UPDATE chatter_facts SET active = 0 WHERE id IN ('
            '  SELECT id FROM chatter_facts WHERE username = ? AND active = 1'
            '  ORDER BY last_seen = ? DESC, hits DESC, last_seen DESC, id DESC LIMIT -1 OFFSET ?)',
            (profile.username, conversation, max_facts),
        )
        await save_profile(profile)


_WORD = re.compile(r'\w{4,}')


def _same(text: str) -> str:
    """A fact's wording with case and punctuation ignored, for spotting a repeat."""
    return ' '.join(re.findall(r'\w+', text.casefold()))


def _stems(text: str) -> set[str]:
    """Word stems for a rough topic match: the first five letters, so «варик» meets «варике»."""
    return {w[:5] for w in _WORD.findall(text.casefold())}


def pick_facts(facts: list[Fact], n: int, query: str = '') -> list[Fact]:
    """n of the facts for one answer: up to two on the question's topic, up to two of
    the freshest, the rest at random – every answer digs up something else."""
    if len(facts) <= n:
        return random.sample(facts, len(facts))
    stems = _stems(query)
    on_topic = [f for f in facts if stems & _stems(f.text)] if stems else []
    picked = random.sample(on_topic, min(2, len(on_topic), n))
    fresh = sorted((f for f in facts if f not in picked), key=lambda f: (f.last_seen, f.id), reverse=True)
    picked += fresh[:min(2, n - len(picked))]
    rest = [f for f in facts if f not in picked]
    picked += random.sample(rest, min(n - len(picked), len(rest)))
    return picked


MEMORY_TABLES = ('chronicles', 'chatter_events', 'chatter_profiles', 'chatter_facts', 'memory_state')


async def clear_memory() -> None:
    async with transaction() as db:
        for table in MEMORY_TABLES:
            await db.execute(f'DELETE FROM {table}')


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
    async with transaction() as db:
        async with db.execute("SELECT fact FROM facts WHERE fact NOT LIKE '%@%' ORDER BY id") as cursor:
            facts = [row[0] for row in await cursor.fetchall()]
        added = 0
        for fact in facts:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO knowledge (content, source) VALUES (?, 'facts')", (fact,),
            )
            added += cursor.rowcount
    if added:
        invalidate_knowledge_cache()
    return added
