"""Writing the memory: a conversation's chronicle, then the profiles of its active chatters.

A conversation is chat between two silences of MEMORY_SILENCE_MINUTES (storage.Block),
told by the messages alone, so a missed stream event, a restart mid-stream or a stream
that ended while the bot was down cannot shift it. memory_loop() picks up finished
conversations every CHECK_SECONDS; the whole history is built once by bot.py
--build-memory (src/cli/memory.py).

The bot's own lines never go in, or it would quote itself and loop on phrases. The
model writes in the chat's own language rather than as a neutral report, because the
profile later sits next to the persona and a polite summary pulls the bot's answers
polite too. Facts must come from the messages, nothing invented.
"""
import asyncio
import logging
from typing import NamedTuple

from google.genai import types
from pydantic import BaseModel, ValidationError

from src.core.config import Gemini, Memory
from src.core.content import Content
from src.core.utils import clean_nick, fix_dashes, gather_cancelling, trim_to_sentence
from src.gemini.client import BLOCK_INPUT, BLOCK_OUTPUT, EMPTY, ERROR, SAFETY_OFF, generate_checked
from src.gemini.memory import storage
from src.gemini.memory.storage import Block, Profile
from src.local.roll.storage import get_session_champion, get_session_loser

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
# The prompt asks for up to 800 characters; the model sometimes writes more
PORTRAIT_MAX_CHARS = 1000

NONE = '–'

# How often the bot looks for a finished conversation
CHECK_SECONDS = 600

# How many Gemini requests the memory may run at once out of GEMINI_CONCURRENCY, which
# it shares with the chat: a split chronicle fans out into many requests and must not
# keep viewers waiting while a stream is live. The CLI backfill lifts it (use_all_slots)
MEMORY_CONCURRENCY = 2
_slots = asyncio.Semaphore(MEMORY_CONCURRENCY)


def use_all_slots() -> None:
    """For bot.py --build-memory: nobody is waiting in chat, use every Gemini slot."""
    global _slots
    _slots = asyncio.Semaphore(Gemini.CONCURRENCY)


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


class _Profile(BaseModel):
    portrait: str
    relations: list[_Relation]


class Chronicle(NamedTuple):
    text: str
    events: list[tuple[str, str]]


def _config(schema: type[BaseModel], max_tokens: int) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        temperature=MEMORY_TEMPERATURE,
        max_output_tokens=max_tokens,
        safety_settings=SAFETY_OFF,
        response_mime_type='application/json',
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


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
        async with _slots:
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


# --- profiles ----------------------------------------------------------------

def _relations_text(relations: list[dict]) -> str:
    return '\n'.join(f'{r["nick"]}: {r["note"]}' for r in relations) or NONE


def _profile_text(profile: Profile | None) -> str:
    if profile is None:
        return NONE
    return f'{profile.portrait}\n\nОтношения:\n{_relations_text(profile.relations)}'


def _events_text(events: list[tuple[str, str]]) -> str:
    return '\n'.join(f'[{s}] {e}' for s, e in events) or NONE


async def _write_profile(username: str, *, old: Profile | None, chronicle: str, events: str,
                         messages: list[str], mentions: list[tuple[str, str]],
                         facts: list[str]) -> tuple[str, list[dict]] | None:
    """The rewritten profile. Retried without mentions, then on fewer of the latest
    messages while Gemini blocks it: other people's lines about someone trip the
    filter as often as their own."""
    attempts = [(messages, mentions)]
    if mentions:
        attempts.append((messages, []))
    attempts += [(messages[-n:], []) for n in PROFILE_RETRY_MESSAGES if n < len(messages)]
    for attempt, (msgs, ments) in enumerate(attempts):
        if attempt:
            logger.info('Память: профиль %s заблокирован фильтром Gemini, повтор на %d последних сообщениях',
                        username, len(msgs))
        prompt = Content.prompt(
            'memory_profile',
            user=username, profile=_profile_text(old), chronicle=chronicle or NONE,
            events=events, messages='\n'.join(msgs) or NONE, mentions=_lines(ments),
            facts='\n'.join(facts) or NONE, max_relations=MAX_RELATIONS,
        )
        try:
            result = await _ask(prompt, _Profile, PROFILE_MAX_TOKENS)
        except _Blocked:
            continue
        # Not a block – fewer messages would not help
        if result is None or not result.portrait.strip():
            return None
        break
    else:
        return None
    # Only people who really write in this chat: the model sometimes turns a
    # name mentioned in passing (a streamer, a politician) into a relation
    candidates = [(clean_nick(r.nick), r.note.strip()) for r in result.relations]
    known = await storage.known_nicks([n for n, _ in candidates if n])
    relations = [
        {'nick': nick, 'note': note} for nick, note in candidates
        if nick in known and nick != username and note
    ]
    return trim_to_sentence(result.portrait.strip(), PORTRAIT_MAX_CHARS), relations[:MAX_RELATIONS]


async def update_profile(username: str, block: Block, chronicle: Chronicle | None) -> Profile | None:
    """Rewrite the chatter's profile with one more conversation. None – skipped or failed.

    A profile already updated with exactly this conversation is left alone, so a
    rerun after a crash does not feed the same conversation in twice.
    """
    old = await storage.get_profile(username)
    if old is not None and old.last_conversation == block.key:
        return None
    messages, mentions, events = await asyncio.gather(
        storage.user_messages(username, PROFILE_CONVERSATION_MESSAGES, block.span),
        storage.mentions_of(username, PROFILE_MENTIONS, block.span),
        storage.user_events(username, PROFILE_EVENTS, before=block.key),
    )
    if chronicle is not None:
        # This conversation's events are not saved yet: the chronicle is written last
        events = events + [(block.key, e) for nick, e in chronicle.events if nick == username]
    written = await _write_profile(
        username, old=old, chronicle=chronicle.text if chronicle else '',
        events=_events_text(events), messages=messages, mentions=mentions, facts=[],
    )
    if written is None:
        logger.warning('Память: профиль %s за %s не обновлён', username, block.key)
        return None
    profile = Profile(username, written[0], written[1], block.key,
                      await storage.sessions_seen(username, block.last_id))
    await storage.save_profile(profile)
    return profile


async def first_profile(username: str, last: Block, *, save: bool = True,
                        extra_events: list[tuple[str, str]] = ()) -> Profile | None:
    """A profile from the chatter's whole history at once – for the backfill.

    One call per chatter instead of replaying every conversation in order:
    cheaper, and the model sees the person whole. Saved facts that name them go
    in as well. last – the last conversation they wrote in, the profile's date.
    """
    messages, mentions, events, facts = await asyncio.gather(
        storage.user_messages(username, BACKFILL_MESSAGES),
        storage.mentions_of(username, BACKFILL_MENTIONS),
        storage.user_events(username, BACKFILL_EVENTS),
        storage.facts_about(username),
    )
    written = await _write_profile(
        username, old=None, chronicle='', events=_events_text(events + list(extra_events)),
        messages=messages, mentions=mentions, facts=facts,
    )
    if written is None:
        logger.warning('Память: первый профиль %s не составлен', username)
        return None
    profile = Profile(username, written[0], written[1], last.key,
                      await storage.sessions_seen(username, last.last_id))
    if save:
        await storage.save_profile(profile)
    return profile


# --- a conversation ------------------------------------------------------------

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
    await storage.save_chronicle(
        block,
        chronicle.text if chronicle else '',
        storage.ChronicleStatus.OK if chronicle else storage.ChronicleStatus.FAILED,
        chronicle.events if chronicle else [],
    )
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
