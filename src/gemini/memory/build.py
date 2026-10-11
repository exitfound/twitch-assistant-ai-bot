"""Writing the memory: a conversation's chronicle, then the dossiers of its active chatters.

A conversation is chat between two silences of MEMORY_SILENCE_MINUTES (storage.Block),
told by the messages alone, so a missed stream event, a restart mid-stream or a stream
that ended while the bot was down cannot shift it. memory_loop() picks up finished
conversations every CHECK_SECONDS; the whole history is built once by bot.py
--build-memory (src/cli/memory.py).

The bot's own lines never go in, or it would quote itself and loop on phrases. The
model writes in the chat's own language rather than as a neutral report, because the
dossier later sits next to the persona and a polite summary pulls the bot's answers
polite too. Facts must come from the messages, nothing invented.

A dossier is a list of retold facts merged after every conversation (new, confirmed,
outdated), plus a headline, relations and at most two catchphrases. A paragraph
rewritten whole gathers quotes, and the bot pastes them back into chat.
"""
import asyncio
import logging
import re
from typing import NamedTuple

from google.genai import types
from pydantic import BaseModel, ValidationError

from src.core.config import Gemini, Memory
from src.core.content import Content
from src.core.utils import clean_nick, gather_cancelling
from src.gemini.client import (
    BLOCK_INPUT, BLOCK_OUTPUT, EMPTY, ERROR, background, generate_checked, make_gen_config,
)
from src.gemini.memory import storage
from src.gemini.memory.storage import FACT_KINDS, HEADLINE_MAX_CHARS, Block, Fact, Profile
from src.gemini.output import fix_dashes, trim_to_sentence
from src.twitch.local.roll.storage import get_session_champion, get_session_loser

logger = logging.getLogger(__name__)

# Precise enough not to make things up, loose enough to keep the chat's voice
MEMORY_TEMPERATURE = 0.9
# How much of a chatter goes into one profile update
PROFILE_CONVERSATION_MESSAGES = 400
PROFILE_MENTIONS = 150
PROFILE_EVENTS = 30
# The first profile, built from the whole history at once (backfill)
BACKFILL_MESSAGES = 400
BACKFILL_MENTIONS = 200
BACKFILL_EVENTS = 60
# Upper bounds the model is asked to keep; the schema cannot enforce lengths
MAX_RELATIONS = 8
# Gemini's input filter (PROHIBITED_CONTENT) cannot be switched off and judges the
# whole request, so a chat it blocks whole passes in halves. A blocked chronicle is
# split down to this many messages per piece, the size at which nothing is blocked
CHRONICLE_MIN_PIECE = 50
# A blocked profile update is retried on ever fewer of the latest messages,
# without the mentions by others
PROFILE_RETRY_MESSAGES = (200, 100, 50)
# Output caps: a model stuck repeating itself stops here instead of running into
# the timeout. A normal chronicle is ~1.5k tokens, a profile ~0.5k
CHRONICLE_MAX_TOKENS = 4096
PROFILE_MAX_TOKENS = 2048
FACT_MAX_CHARS = 200
# A dynamic budget (-1) has no number: room for the most Flash thinks on its own
DYNAMIC_THINKING_TOKENS = 24576
# New facts taken from one update: a model listing every message is not a dossier.
# prompts.memory_dossier names the same number (tests/test_content.py checks it)
MAX_NEW_FACTS = 15
MAX_CATCHPHRASES = 2
CATCHPHRASE_MAX_CHARS = 80
# Retold facts carry no quotes: a quoted phrase is what the bot pastes back
_QUOTES = re.compile('[«»“”„"]')

NONE = '–'

# How often the bot looks for a finished conversation
CHECK_SECONDS = 600

class _Event(BaseModel):
    nick: str
    event: str


class _Chronicle(BaseModel):
    summary: str
    events: list[_Event]


class _Merged(BaseModel):
    summary: str


class _Relation(BaseModel):
    nick: str
    note: str


class _NewFact(BaseModel):
    kind: str
    text: str


class _Dossier(BaseModel):
    headline: str
    new_facts: list[_NewFact]
    confirmed: list[int]
    outdated: list[int]
    relations: list[_Relation]
    catchphrases: list[str]


class Dossier(NamedTuple):
    """One update of a chatter's dossier, ready for storage.apply_dossier()."""
    profile: Profile
    new: list[tuple[str, str]]
    confirmed: list[int]
    outdated: list[int]


class Chronicle(NamedTuple):
    text: str
    events: list[tuple[str, str]]


def _config(schema: type[BaseModel], max_tokens: int) -> types.GenerateContentConfig:
    """No persona, JSON by the schema, the output capped. GEMINI_THINKING_BUDGET applies as
    everywhere: a model that only works with thinking (Pro) rejects a budget of 0. Thinking
    counts against max_output_tokens, so the cap grows by the budget and the answer keeps
    max_tokens of its own."""
    config = make_gen_config(persona=False, temperature=MEMORY_TEMPERATURE)
    budget = Gemini.THINKING_BUDGET
    config.max_output_tokens = max_tokens + (DYNAMIC_THINKING_TOKENS if budget < 0 else budget)
    config.response_mime_type = 'application/json'
    config.response_schema = schema
    return config


class _Blocked(Exception):
    """Gemini's input filter refused the prompt – a smaller one may pass."""


class Unavailable(Exception):
    """Gemini did not answer at all (timeout, network, quota) – try later, not smaller.

    Kept apart from a block on purpose: a conversation the filter refuses for
    good must be marked done, or it would stop every later one behind it.
    """


async def _ask(prompt: str, schema: type[BaseModel], max_tokens: int) -> BaseModel | None:
    """The parsed answer. None – no usable answer (unparseable, empty, cut by the output
    filter or rejected by the API twice); _Blocked – the input filter refused;
    Unavailable – no answer at all (timeout, network, server errors).

    Asked for once more when the answer is unparseable (the model occasionally loops
    on one phrase until max_tokens cuts the JSON off) or stopped by the output filter,
    which is random enough that the same prompt usually passes on the next try.
    """
    for attempt in range(2):
        async with background():
            text, block = await generate_checked(prompt, _config(schema, max_tokens))
        if block == BLOCK_INPUT:
            raise _Blocked
        if block in (BLOCK_OUTPUT, EMPTY, ERROR):
            # Answered with nothing, or the API rejected the request: one more try, then it
            # counts as a failure – a prompt that always comes back empty (RECITATION) or
            # rejected (a 4xx) must not hold up the conversation and cost a call every check
            logger.warning('Память: Gemini ответил пусто (%s, попытка %d)', block, attempt + 1)
            continue
        if not text:
            raise Unavailable
        # The model likes the em dash; the project uses only the en dash
        text = fix_dashes(text)
        try:
            return schema.model_validate_json(text)
        except ValidationError:
            logger.warning('Память: ответ Gemini не разобран (попытка %d): %.200s', attempt + 1, text)
    return None


def _lines(rows: list[tuple[str, str]]) -> str:
    return '\n'.join(f'{u}: {m}' for u, m in rows) or NONE


async def _game_line(block: Block) -> str:
    session_id = await storage.block_session(block)
    if session_id is None:
        return NONE
    loser, champion = await asyncio.gather(
        get_session_loser(session_id), get_session_champion(session_id),
    )
    parts = []
    if loser:
        parts.append(f'залупа стрима – {loser[0]} ({loser[1]})')
    if champion and (not loser or champion[0] != loser[0]):
        parts.append(f'китежанин стрима – {champion[0]} ({champion[1]})')
    return ', '.join(parts) or NONE


# --- chronicle ---------------------------------------------------------------

async def _chronicle_pieces(key: str, chat: list[tuple[str, str]],
                            game: str) -> tuple[list[str], list[_Event]] | None:
    """Summaries and events of the chat, split in halves while Gemini blocks it."""
    prompt = Content.prompt(
        'memory_chronicle',
        started=key, count=len(chat), game=game, chat=_lines(chat),
    )
    try:
        result = await _ask(prompt, _Chronicle, CHRONICLE_MAX_TOKENS)
    except _Blocked:
        result = None
    else:
        # Not a block (timeout, network, garbage) – splitting would not help
        if result is None or not result.summary.strip():
            return None
        return [result.summary.strip()], list(result.events)
    if len(chat) < 2 * CHRONICLE_MIN_PIECE:
        return None
    half = len(chat) // 2
    logger.info('Память: хроника %s заблокирована фильтром Gemini, делю (%d сообщений)', key, len(chat))
    halves = (chat[:half], chat[half:])
    results = await gather_cancelling(*(_chronicle_pieces(key, h, game) for h in halves))
    for h, result in zip(halves, results, strict=True):
        if result is None:
            # The chronicle goes on without this part and its people's events
            logger.warning('Память: хроника %s – кусок из %d сообщений не прошёл, пишу без него',
                           key, len(h))
    parts = [r for r in results if r is not None]
    if not parts:
        return None
    return [t for texts, _ in parts for t in texts], [e for _, events in parts for e in events]


async def _merge(key: str, texts: list[str]) -> str:
    """One chronicle out of the pieces of a split one; the pieces joined if that fails.

    Joined pieces run 3–5 times longer than a normal chronicle and repeat each
    other. The summaries alone are small and rarely trip the input filter.
    """
    joined = '\n\n'.join(texts)
    if len(texts) < 2:
        return joined
    parts = '\n\n'.join(f'--- Часть {i} ---\n{t}' for i, t in enumerate(texts, 1))
    prompt = Content.prompt('memory_chronicle_merge', started=key, parts=parts)
    try:
        result = await _ask(prompt, _Merged, CHRONICLE_MAX_TOKENS)
    except (_Blocked, Unavailable):
        # The pieces are already paid for: an outage on the merge must not throw them away
        result = None
    if result is None or not result.summary.strip():
        logger.warning('Память: хроника %s не сведена, оставляю %d кусков', key, len(texts))
        return joined
    return result.summary.strip()


async def write_chronicle(block: Block) -> Chronicle | None:
    """The conversation's chronicle and per-chatter events. None – Gemini gave nothing usable."""
    chat = await storage.block_chat(block)
    if not chat:
        return None
    result = await _chronicle_pieces(block.key, chat, await _game_line(block))
    if result is None:
        return None
    texts, items = result
    # Only nicks that really wrote in this conversation: the model sometimes
    # turns a nick mentioned in passing into a participant
    present = {u for u, _ in chat}
    events = []
    for item in items:
        nick = clean_nick(item.nick)
        if nick in present and item.event.strip():
            events.append((nick, item.event.strip()))
    return Chronicle(await _merge(block.key, texts), events)


# --- dossiers ------------------------------------------------------------------

def _relations_text(relations: list[dict]) -> str:
    return '\n'.join(f'{r["nick"]}: {r["note"]}' for r in relations) or NONE


def _dossier_text(profile: Profile | None, facts: list[Fact]) -> str:
    """The dossier as the model sees it: facts with their ids, so it can confirm or
    retire them instead of writing them again."""
    if profile is None and not facts:
        return NONE
    lines = [f'Кто он: {profile.portrait if profile else NONE}', 'Факты:']
    lines += [f'[{f.id}] ({f.kind}) {f.text}' for f in facts] or [NONE]
    lines += ['Коронные фразы:', *((profile.catchphrases if profile else ()) or [NONE])]
    lines += ['Отношения:', _relations_text(profile.relations if profile else [])]
    return '\n'.join(lines)


def _events_text(events: list[tuple[str, str]]) -> str:
    return '\n'.join(f'[{s}] {e}' for s, e in events) or NONE


def _retold(text: str, limit: int) -> str:
    """A fact or a headline without quote marks, cut at a sentence end."""
    return trim_to_sentence(re.sub(r'\s+', ' ', _QUOTES.sub('', text)).strip(), limit)


async def _write_dossier(username: str, *, profile: Profile | None, facts: list[Fact], chronicle: str,
                         events: str, messages: list[str], mentions: list[tuple[str, str]],
                         notes: list[str]) -> _Dossier | None:
    """The model's update of the dossier. Retried without mentions, then on fewer of the
    latest messages while Gemini blocks it: other people's lines about someone trip the
    filter as often as their own."""
    attempts = [(messages, mentions)]
    if mentions:
        attempts.append((messages, []))
    attempts += [(messages[-n:], []) for n in PROFILE_RETRY_MESSAGES if n < len(messages)]
    for attempt, (msgs, ments) in enumerate(attempts):
        if attempt:
            logger.info('Память: досье %s заблокировано фильтром Gemini, повтор на %d последних сообщениях',
                        username, len(msgs))
        prompt = Content.prompt(
            'memory_dossier',
            user=username, dossier=_dossier_text(profile, facts), chronicle=chronicle or NONE,
            events=events, messages='\n'.join(msgs) or NONE, mentions=_lines(ments),
            facts='\n'.join(notes) or NONE, max_relations=MAX_RELATIONS,
        )
        try:
            result = await _ask(prompt, _Dossier, PROFILE_MAX_TOKENS)
        except _Blocked:
            continue
        # Not a block – fewer messages would not help
        if result is None or not result.headline.strip():
            return None
        return result
    return None


async def _dossier(username: str, key: str, sessions: int, result: _Dossier,
                   facts: list[Fact]) -> Dossier:
    """The model's answer, checked: known kinds, no quotes, own fact ids, real nicks."""
    new = []
    for item in result.new_facts[:MAX_NEW_FACTS]:
        text = _retold(item.text, FACT_MAX_CHARS)
        if len(text) >= 5:
            new.append((item.kind if item.kind in FACT_KINDS else 'who', text))
    own = {f.id for f in facts}
    phrases = []
    for phrase in result.catchphrases:
        phrase = _QUOTES.sub('', phrase).strip()
        if phrase and len(phrase) <= CATCHPHRASE_MAX_CHARS and phrase not in phrases:
            phrases.append(phrase)
    # Only people who really write in this chat: the model sometimes turns a
    # name mentioned in passing (a streamer, a politician) into a relation
    candidates = [(clean_nick(r.nick), r.note.strip()) for r in result.relations]
    known = await storage.known_nicks([n for n, _ in candidates if n])
    relations = [
        {'nick': nick, 'note': note} for nick, note in candidates
        if nick in known and nick != username and note
    ][:MAX_RELATIONS]
    profile = Profile(username, _retold(result.headline, HEADLINE_MAX_CHARS), relations, key, sessions,
                      tuple(phrases[:MAX_CATCHPHRASES]))
    return Dossier(profile, new, [i for i in result.confirmed if i in own],
                   [i for i in result.outdated if i in own])


async def update_profile(username: str, block: Block, chronicle: Chronicle | None) -> Profile | None:
    """Merge one more conversation into the chatter's dossier. None – skipped or failed.

    A dossier already updated with exactly this conversation is left alone, so a rerun
    after a crash does not feed the same conversation in twice. A chatter without a
    dossier yet – no headline and no fact – gets one from the whole history up to this
    conversation instead.
    """
    old, facts = await asyncio.gather(storage.get_profile(username), storage.get_facts(username))
    if old is not None and old.last_conversation == block.key:
        return None
    this = [(block.key, e) for nick, e in chronicle.events if nick == username] if chronicle else []
    if not facts and not storage.has_headline(old):
        dossier = await first_profile(username, block, extra_events=this)
        return dossier.profile if dossier else None
    messages, mentions, events = await asyncio.gather(
        storage.user_messages(username, PROFILE_CONVERSATION_MESSAGES, block.span),
        storage.mentions_of(username, PROFILE_MENTIONS, block.span),
        storage.user_events(username, PROFILE_EVENTS, before=block.key),
    )
    # This conversation's events are not saved yet: the chronicle is written last
    result = await _write_dossier(
        username, profile=old, facts=facts, chronicle=chronicle.text if chronicle else '',
        events=_events_text(events + this), messages=messages, mentions=mentions, notes=[],
    )
    if result is None:
        logger.warning('Память: досье %s за %s не обновлено', username, block.key)
        return None
    dossier = await _dossier(username, block.key, await storage.sessions_seen(username, block.last_id),
                             result, facts)
    await storage.apply_dossier(*dossier, Memory.DOSSIER_MAX_FACTS)
    return dossier.profile


async def first_profile(username: str, last: Block, *, save: bool = True,
                        extra_events: list[tuple[str, str]] = ()) -> Dossier | None:
    """A dossier from the chatter's whole history at once – for the backfill.

    One call per chatter instead of replaying every conversation in order:
    cheaper, and the model sees the person whole. Saved facts that name them go
    in as well. last – the last conversation they wrote in, the dossier's date: nothing
    after it goes in, or the conversations after it would feed the same messages again.
    Facts a chatter already has are switched off: this dossier replaces them.
    """
    upto = (0, last.last_id)
    messages, mentions, events, notes, old = await asyncio.gather(
        storage.user_messages(username, BACKFILL_MESSAGES, upto),
        storage.mentions_of(username, BACKFILL_MENTIONS, upto),
        storage.user_events(username, BACKFILL_EVENTS, before=last.key),
        storage.facts_about(username),
        storage.get_facts(username),
    )
    result = await _write_dossier(
        username, profile=None, facts=[], chronicle='', events=_events_text(events + list(extra_events)),
        messages=messages, mentions=mentions, notes=notes,
    )
    if result is None:
        logger.warning('Память: первое досье %s не составлено', username)
        return None
    dossier = await _dossier(username, last.key, await storage.sessions_seen(username, last.last_id),
                             result, [])
    if save:
        await storage.apply_dossier(dossier.profile, dossier.new, [], [f.id for f in old],
                                    Memory.DOSSIER_MAX_FACTS)
    return dossier


# --- a conversation ------------------------------------------------------------

async def save_result(block: Block, chronicle: Chronicle | None) -> None:
    """Mark a conversation done: its chronicle, or a failed row – --build-memory retries those."""
    await storage.save_chronicle(
        block,
        chronicle.text if chronicle else '',
        storage.ChronicleStatus.OK if chronicle else storage.ChronicleStatus.FAILED,
        chronicle.events if chronicle else [],
    )


async def process_block(block: Block) -> bool:
    """Chronicle and profile updates for one finished conversation. False – Gemini is down.

    The chronicle row is written last and marks the conversation done, so a bot that
    stops halfway processes it again and update_profile() skips the profiles already
    updated with it. Unavailable (timeout, network, quota) leaves the conversation
    unmarked for the next check; anything else is written as failed, so one hopeless
    conversation never holds up the ones after it.
    """
    if block.count < Memory.CONVERSATION_MIN_MESSAGES:
        await storage.save_chronicle(block, '', storage.ChronicleStatus.SKIPPED, [])
        logger.info('Память: разговор %s – %d сообщений, мало для хроники', block.key, block.count)
        return True
    try:
        chronicle = await write_chronicle(block)
        if chronicle is None:
            logger.warning('Память: хроника %s не получена, профили обновляю без неё', block.key)
        chatters = await storage.active_chatters(block, Memory.PROFILE_MIN_MESSAGES)
        updated = 0
        for username in chatters:
            if await update_profile(username, block, chronicle) is not None:
                updated += 1
    except Unavailable:
        logger.warning('Память: Gemini не отвечает, разговор %s повторю в следующий раз', block.key)
        return False
    await save_result(block, chronicle)
    logger.info('Память: разговор %s (%d сообщений) – хроника %s, профилей обновлено %d из %d',
                block.key, block.count, 'есть' if chronicle else 'нет', updated, len(chatters))
    return True


async def pending_blocks() -> list[Block]:
    """Finished conversations the memory has not seen yet, oldest first.

    Empty until bot.py --build-memory has finished: the history is its job, and a
    bot must not quietly replay months of chat, nor race a build still running.
    """
    if not await storage.memory_built():
        return []
    return await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=True)


_lock = asyncio.Lock()


async def catch_up() -> None:
    """Process every finished conversation the memory has not seen yet. Never raises."""
    async with _lock:
        try:
            if not await storage.memory_built():
                logger.info('Память не собрана – историю строит bot.py --build-memory, '
                            'дальше бот ведёт её сам')
                return
            for block in await pending_blocks():
                if not await process_block(block):
                    # Gemini is not answering – the rest would fail the same way
                    break
        except Exception:
            logger.exception('Память: обработка разговоров прервана')


async def memory_loop() -> None:
    """Every CHECK_SECONDS write the memory of conversations that have finished.

    Runs from bot start, so conversations that ended while the bot was down are
    written right away. Cancelled only with the bot: a conversation interrupted
    halfway is processed again next time, its done profiles are skipped.
    """
    if not Memory.ENABLED:
        return
    logger.info('Память: разговор закончен после %d мин тишины, проверка раз в %d мин',
                Memory.SILENCE_MINUTES, CHECK_SECONDS // 60)
    try:
        while True:
            await catch_up()
            await asyncio.sleep(CHECK_SECONDS)
    finally:
        logger.info('Память: проверка остановлена')
