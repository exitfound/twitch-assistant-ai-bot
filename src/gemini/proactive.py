"""Proactive remarks: the bot writes to chat on its own once per interval."""
import asyncio
import logging
import random
import re

from src.core.activity import ChatWatch
from src.core.config import Context, Proactive
from src.core.content import Content
from src.core.database import get_random_knowledge, get_recent_chat, save_bot_interaction
from src.core.utils import TWITCH_MSG_MAX, random_delay
from src.gemini.client import generate, make_gen_config
from src.gemini.context import ContextBuilder
from src.gemini.responder import apply_caps, maybe_add_emote, passes_moderation

logger = logging.getLogger(__name__)


async def proactive_loop(bot) -> None:
    """A periodic remark to chat on the bot's own initiative."""
    watch = ChatWatch()
    try:
        while True:
            delay = random_delay(Proactive.INTERVAL_MIN_MINUTES, Proactive.INTERVAL_MAX_MINUTES)
            logger.debug('Следующая проактивная реплика через %.1f мин', delay / 60)
            await asyncio.sleep(delay)
            try:
                await _send_proactive(bot, watch)
            except Exception:
                logger.exception('Проактивное сообщение не отправлено')
    except asyncio.CancelledError:
        raise
    finally:
        logger.info('Цикл проактивных сообщений остановлен')


async def _send_proactive(bot, watch: ChatWatch) -> None:
    session_id = bot.session_id
    # Only into a live conversation: nobody has written since the previous
    # remark – a new one would be the bot talking to itself
    if not await watch.new_messages(session_id):
        return
    recent_chat = await get_recent_chat(session_id, Context.CHAT_MESSAGES)
    if not recent_chat:
        return

    random_knowledge = await get_random_knowledge(Context.KNOWLEDGE_RANDOM)
    active_users = list({u for u, _ in recent_chat[-Proactive.ACTIVE_WINDOW:]})
    target_user = random.choice(active_users) if active_users else None

    if target_user and random.random() < Proactive.TARGET_PROBABILITY:
        event_prompt = Content.prompt('proactive_user', user=target_user)
    else:
        event_prompt = Content.prompt('proactive_general')

    prompt_ctx = (
        ContextBuilder()
        .add_chat(Content.label('chat'), recent_chat)
        .add_lines(Content.label('language'), random_knowledge)
        .add_raw(event_prompt)
    )
    text = await generate(prompt_ctx.build(), make_gen_config())
    if not text:
        return

    text = re.sub(r'\s{2,}', ' ', text).strip()
    if len(text) > TWITCH_MSG_MAX:
        text = text[:TWITCH_MSG_MAX - 3] + '...'
    if not passes_moderation(text):
        return
    text = apply_caps(text)
    text = maybe_add_emote(text)
    if await bot.send_chat_message(text):
        await save_bot_interaction(session_id, '_proactive_', event_prompt, text)
