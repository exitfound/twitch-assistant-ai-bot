"""Has anyone talked in chat since a loop last looked.

The bot's own messages (reminder, proactive remarks, emote spam) go out only into a
live conversation: if nobody has written since the previous one, a new one would be
the bot talking to itself. Only live dialogue counts – commands to the bot are not
stored in chat_messages.
"""
from src.core.database import get_chat_after


class ChatWatch:
    """Remembers the last chat message one loop has seen.

    Message ids run through all sessions, so a change of stream breaks nothing:
    the first message of the new session is simply newer.
    """

    def __init__(self) -> None:
        self._seen = 0

    async def new_messages(self, session_id: str) -> bool:
        """True – someone wrote in the session since the previous call that returned True."""
        last_id = await get_chat_after(session_id, self._seen)
        if last_id is None:
            return False
        self._seen = last_id
        return True
