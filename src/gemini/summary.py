"""!summary: the stream so far, or the previous stream (owner, 2026-09-19).

During a stream the bot retells the whole stream, not its last 500 messages – the
longest ones ran past 800 and the start was lost. Gemini's input filter judges a
request by combinations of messages, and with hundreds of them it blocks a
fraction of the chats whole (12 of 89 chronicles in the memory backfill), so a
blocked request is asked again with less (answer_context.walk()): the whole
stream → the last CONTEXT_SUMMARY_MESSAGES → the last FALLBACK_MESSAGES.

Offline the session is a date and there is next to no chat in it, so there – and
during a stream with «!summary прошлый» – the bot retells the previous stream from
its chronicle in the memory: a few hundred characters, almost free. Right after a
stream the chronicle is not written yet (the memory waits for MEMORY_SILENCE_MINUTES
of silence), so then the previous stream's chat itself is retold, with the ladder.

Limited per viewer and stream like !who and !versus (_per_stream() in commands.py):
a follower SUMMARY_PER_STREAM_FOLLOWER, a VIP SUMMARY_PER_STREAM_VIP, a subscriber
or moderator SUMMARY_PER_STREAM_SUB, the broadcaster unlimited; the current and the
previous stream share one count (owner, 2026-09-20).
"""

from google.genai import types

from src.core.config import Context, Memory
from src.core.content import Content
from src.core.database import get_last_chat_session, get_recent_chat
from src.gemini.answer_context import is_stream_session, walk
from src.gemini.client import make_gen_config
from src.gemini.memory import storage

TEMPERATURE = 1.2
# The last rung of the ladder: a chat this short passed the filter in the memory backfill
FALLBACK_MESSAGES = 200

NOW = 'now'
PREVIOUS = 'previous'
# «!summary прошлый», «!summary прошлого стрима» – anything starting like this
PREVIOUS_ARG = 'прош'

# The kind summaries are recorded under in bot_uses: the per-stream limit counts it
KIND = 'summary'


def mode(args: str, session_id: str) -> str:
    if args.strip().lower().startswith(PREVIOUS_ARG) or not is_stream_session(session_id):
        return PREVIOUS
    return NOW


def config() -> types.GenerateContentConfig:
    return make_gen_config(
        system=f"{Content.prompt('system')}\n\n{Content.prompt('summary')}",
        temperature=TEMPERATURE,
    )


async def _chat(session_id: str, request: str, user: str) -> tuple[str | None, str] | None:
    """(answer, rung) for a session's chat down the ladder; None when it has no chat."""
    chat = await get_recent_chat(session_id, Context.STREAM_MAX_MESSAGES)
    if not chat:
        return None
    rungs, seen = [], set()
    for name, n in (('весь стрим', len(chat)),
                    (f'последние {Context.SUMMARY_MESSAGES}', Context.SUMMARY_MESSAGES),
                    (f'последние {FALLBACK_MESSAGES}', FALLBACK_MESSAGES)):
        part = chat[-n:]
        if len(part) in seen:
            continue
        seen.add(len(part))
        head = Content.prompt(request, session_id=session_id, count=len(part))
        chat_text = '\n'.join(f'{u}: {m}' for u, m in part)
        rungs.append((name, f"{head}\n\n{chat_text}\n\n{Content.prompt('summary_tail')}"))
    return await walk(rungs, config(), user)


async def now(session_id: str, user: str) -> tuple[str | None, str] | None:
    """(answer, rung) for the stream so far; None when the stream has no chat yet."""
    return await _chat(session_id, 'summary_request', user)


async def previous(session_id: str, user: str) -> tuple[str | None, str] | None:
    """(answer, rung) for the previous stream: from its chronicle, or from its chat
    while the chronicle is not written yet. None when there is no previous stream."""
    # The latest other session with a real chat: the previous stream both offline and
    # during a stream, even before anyone has written in the current one
    target = await get_last_chat_session(session_id, Memory.CONVERSATION_MIN_MESSAGES)
    if target is None:
        return None
    chronicle = await storage.session_chronicle(target)
    if chronicle:
        prompt = f"{Content.prompt('summary_previous', chronicle=chronicle)}\n\n{Content.prompt('summary_tail')}"
        return await walk([('хроника', prompt)], config(), user)
    return await _chat(target, 'summary_request_previous', user)
