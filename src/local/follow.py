"""Reply to a new follow: a random template from lists.follow, no Gemini."""
import logging
import random

import twitchio

from src.core.content import Content
from src.core.database import save_bot_interaction
from src.core.port import BotPort
from src.core.utils import safe_format

logger = logging.getLogger(__name__)


async def handle_follow(bot: BotPort, payload: twitchio.ChannelFollow) -> None:
    try:
        messages = Content.items('follow')
        if not messages:
            logger.warning('Список фолов в CONTENT.md пуст – на фолов не отвечаем')
            return
        user = payload.user.name
        text = safe_format(random.choice(messages), user=user)
        await payload.respond(text)
        await save_bot_interaction(bot.session_id, user, '[follow]', text)
    except Exception:
        logger.exception('Обработка фолова не удалась')
