"""Reply to a new follow: a random template from lists.follow, no Gemini."""
import collections
import logging
import random
import time

import twitchio

from src.core.content import Content
from src.core.database import save_bot_interaction, was_greeted
from src.core.port import BotPort
from src.core.utils import find_banned, safe_format

logger = logging.getLogger(__name__)

# A follow-bot raid brings hundreds of follows a minute: past this many thanks a minute
# the rest go unanswered. Ordinary load is about two follows a stream
GREETINGS_PER_MINUTE = 3

# When the last greetings went out, on the monotonic clock
_sent_at: collections.deque[float] = collections.deque(maxlen=GREETINGS_PER_MINUTE)

# Logins thanked since the start: a follow event may come twice, and the database check
# alone would let both through before the first one is saved
_greeted: set[str] = set()


async def handle_follow(bot: BotPort, payload: twitchio.ChannelFollow) -> None:
    """Thank a new follower once, ever. Unfollowing and following again gets nothing,
    nor does a login with a stop-list word in it: the bot would say it in chat."""
    try:
        messages = Content.items('follow')
        if not messages:
            logger.warning('Список фолов в CONTENT.md пуст – на фолов не отвечаем')
            return
        user = payload.user.name
        if not user or user.lower() in _greeted:
            return
        # Claimed before any await: every event runs in its own task, and a second delivery
        # of the same follow stops at the check above
        _greeted.add(user.lower())
        if find_banned(user, Content.items('banned')):
            logger.warning('Ник фоловера %s в стоп-листе – без приветствия', user)
            return
        if await was_greeted(user):
            return
        # A slot of the per-minute cap goes only to a greeting about to be sent; checked and
        # taken with no await in between
        now = time.monotonic()
        if len(_sent_at) == GREETINGS_PER_MINUTE and now - _sent_at[0] < 60:
            _greeted.discard(user.lower())      # not thanked: a later follow still may be
            logger.warning('Фолов больше %d в минуту – %s без приветствия', GREETINGS_PER_MINUTE, user)
            return
        _sent_at.append(now)
        text = safe_format(random.choice(messages), user=user)
        if await bot.send_chat_message(text):
            await save_bot_interaction(bot.session_id, user, '[follow]', text)
    except Exception:
        logger.exception('Обработка фолова не удалась')
