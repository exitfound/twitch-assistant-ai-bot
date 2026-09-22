"""!summary: the stream so far, or the previous stream.

During a stream the whole stream is retold rather than a fixed window, because the
longest ones run past 800 messages and a window loses the start. Gemini's input filter
judges a request by combinations of messages and blocks a fraction of long chats whole,
so a blocked request is asked again with less (ladder.walk()): the whole stream
→ the last CONTEXT_SUMMARY_MESSAGES → the last FALLBACK_MESSAGES.

Offline, and on «!summary прошлый», the previous stream is retold from its chronicle in
the memory – a few hundred characters, almost free. The chronicle appears only after
MEMORY_SILENCE_MINUTES of silence, so until then that stream's chat is retold instead,
with the same ladder.

Limited per viewer and stream like !who and !versus (LIMITS in commands.py), the
current and the previous stream sharing one count.
"""

from google.genai import types

from src.core.config import Context, Memory
from src.core.content import Content
from src.core.database import get_last_chat_session, get_recent_chat
from src.gemini.answer_context import is_stream_session
from src.gemini.client import make_gen_config
from src.gemini.context import ContextBuilder
from src.gemini.ladder import unique_rungs, walk
from src.gemini.memory import storage

TEMPERATURE = 1.2
# The last rung of the ladder: a chat this short passes the input filter
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
    rungs = []
    for name, n in (('весь стрим', len(chat)),
                    (f'последние {Context.SUMMARY_MESSAGES}', Context.SUMMARY_MESSAGES),
                    (f'последние {FALLBACK_MESSAGES}', FALLBACK_MESSAGES)):
        part = chat[-n:]
        prompt = (ContextBuilder()
                  .add_raw(Content.prompt(request, session_id=session_id, count=len(part)))
                  .add_pairs(None, part)
                  .add_raw(Content.prompt('summary_tail'))
                  .build())
        rungs.append((name, prompt))
    return await walk(unique_rungs(rungs), config(), user)


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
