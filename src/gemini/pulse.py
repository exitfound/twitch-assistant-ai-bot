"""The pulse: what the chat is about right now, retold in a few lines.

Free-text answers and proactive remarks see the pulse instead of the raw chat of the
stream: raw lines are what the model pastes back in quotes, a retelling gives it the
gist to put in its own words. Written without the persona, in the chat's language and
without quotes. The bot's own lines are not in chat_messages, so it never retells itself.

A platform loop refreshes it while people write (refresh()); readers take the cached
one (current()), which goes stale after two intervals or with a new session. The
context probe builds one for a past moment (build() with before_id).
"""
import logging
import re
import time

from src.core.config import Pulse
from src.core.content import Content
from src.core.database import get_recent_chat
from src.gemini.client import background, generate, make_gen_config
from src.gemini.output import fix_dashes, trim_to_sentence

logger = logging.getLogger(__name__)

# A retelling, not the persona: close to the chat, without invention
TEMPERATURE = 0.7
# Fewer messages than this hold no topic worth retelling
MIN_MESSAGES = 10
# The prompt asks for 3–5 short lines; a runaway answer is cut here
MAX_CHARS = 700

# (session_id, monotonic time it was built, text)
_pulse: tuple[str, float, str] | None = None


async def build(session_id: str, before_id: int | None = None) -> str | None:
    """A fresh pulse of the session's last Pulse.MESSAGES messages; None – too little
    chat or no answer from Gemini. before_id – only the chat before that message."""
    chat = await get_recent_chat(session_id, Pulse.MESSAGES, before_id)
    if len(chat) < MIN_MESSAGES:
        return None
    prompt = Content.prompt('pulse', chat='\n'.join(f'{u}: {t}' for u, t in chat))
    # A background request: it shares the memory's slots, not the ones viewers wait on
    async with background():
        text = await generate(prompt, make_gen_config(persona=False, temperature=TEMPERATURE))
    if not text:
        return None
    lines = [re.sub(r'^[\s*#>•-]+', '', line).replace('*', '').strip() for line in text.splitlines()]
    text = fix_dashes('\n'.join(line for line in lines if line))
    return trim_to_sentence(text, MAX_CHARS) or None


async def refresh(session_id: str) -> bool:
    """Rebuild the cached pulse. False – nothing built (too little chat, no answer): the
    previous one stays until it goes stale."""
    global _pulse
    text = await build(session_id)
    if not text:
        return False
    _pulse = (session_id, time.monotonic(), text)
    logger.debug('Пульс чата обновлён: %s', text.replace('\n', ' | '))
    return True


def current(session_id: str) -> str | None:
    """The cached pulse of this session, if it is no older than two intervals."""
    if _pulse is None:
        return None
    session, built_at, text = _pulse
    if session != session_id or time.monotonic() - built_at > 2 * Pulse.INTERVAL_MINUTES * 60:
        return None
    return text
