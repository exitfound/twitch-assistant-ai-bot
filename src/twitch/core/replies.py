"""Replies in Twitch chat: sending one that never raises, and recognising a reply to the bot."""
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import twitchio

logger = logging.getLogger(__name__)


def reply_to_bot(message: 'twitchio.ChatMessage', bot_id: str | int | None) -> str | None:
    """The bot's line this chat message replies to, or None if it is not a reply to the bot.

    twitchio's ChatMessageReply carries parent_user (a PartialUser), not
    parent_user_id: reading the latter yields '' and no reply is ever recognised,
    which only stays invisible because Twitch puts @botname at the start of a reply.
    """
    reply = getattr(message, 'reply', None)
    parent = getattr(reply, 'parent_user', None)
    if parent is None or str(getattr(parent, 'id', '')) != str(bot_id):
        return None
    return getattr(reply, 'parent_message_body', None) or None


async def reply(message: 'twitchio.ChatMessage', text: str) -> bool:
    """Reply in chat; True if Twitch took the message. Never raises.

    A refusal or a fallback line must not fail the handler that sends it: when Twitch is
    unreachable the error is logged once here. Twitch may also drop a message (AutoMod,
    a duplicate) and report it with sent=False.
    """
    try:
        result = await message.respond(text)
    except Exception as e:
        logger.warning('Ответ в чат не ушёл: %s', e)
        return False
    if getattr(result, 'sent', True) is False:
        logger.warning('Twitch отбросил ответ (%s): %r', getattr(result, 'dropped_code', None), text[:80])
        return False
    return True
