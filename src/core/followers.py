"""Whether a viewer follows the channel: a Twitch request with a cache.

A chat event carries no follower badge – unlike subscriber, VIP and
moderator – so the status has to be asked from Helix
(`channels/followers`, scope `moderator:read:followers`, the same one needed
for the follow subscription). A request per chat message would be wasteful,
so the answer lives in the cache for FOLLOW_CACHE_MINUTES.

A request error is read in the viewer's favour: a silent Twitch must not
lock the chat out of the bot.
"""
import logging
import time

from src.core.config import Follow

logger = logging.getLogger(__name__)


class FollowerCache:

    def __init__(self) -> None:
        # user_id -> (is a follower, until when the cache is trusted)
        self._cache: dict[str, tuple[bool, float]] = {}

    def forget(self, user_id: str) -> None:
        """Forget the answer: called when a new-follow event arrives."""
        self._cache.pop(str(user_id), None)

    async def is_follower(self, bot, user_id: str) -> bool:
        user_id = str(user_id)
        cached = self._cache.get(user_id)
        now = time.time()
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            result = await self._fetch(bot, user_id)
        except Exception:
            logger.warning('Не удалось узнать, фолловер ли %s – считаю, что да', user_id, exc_info=True)
            return True
        self._cache[user_id] = (result, now + Follow.CACHE_MINUTES * 60)
        if len(self._cache) > 1000:
            self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
        return result

    async def _fetch(self, bot, user_id: str) -> bool:
        broadcaster = bot.create_partialuser(bot.channel_id)
        followers = await broadcaster.fetch_followers(user=user_id, token_for=str(bot.bot_id))
        # followers.followers is an async iterator with neither __bool__
        # nor __len__, so bool() of it is always true and the check would silently
        # let everyone in. Unwrap the first page: with user= Twitch returns
        # either one record or an empty list
        return bool(await followers.followers)
