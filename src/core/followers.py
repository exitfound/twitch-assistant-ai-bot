"""Фолловер ли зритель: запрос к Twitch с кэшем.

Значков фолловера в событии чата нет – в отличие от подписки, випки и
модератора, – поэтому статус приходится спрашивать у Helix
(`channels/followers`, право `moderator:read:followers`, оно же нужно для
подписки на фоловы). Запрос на каждое сообщение чата был бы расточительным,
поэтому ответ живёт в кэше FOLLOW_CACHE_MINUTES.

Ошибку запроса трактуем в пользу зрителя: молчащий Twitch не должен
закрывать чату доступ к боту.
"""
import logging
import time

from src.core.config import Follow

logger = logging.getLogger(__name__)


class FollowerCache:

    def __init__(self) -> None:
        # user_id -> (фолловер ли, до какого времени верим кэшу)
        self._cache: dict[str, tuple[bool, float]] = {}

    def forget(self, user_id: str) -> None:
        """Забыть ответ: зовём, когда пришло событие о новом фолове."""
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
        # followers.followers – асинхронный итератор, у него нет ни __bool__,
        # ни __len__, поэтому bool() от него истина всегда и проверка молча
        # пускала бы каждого. Разворачиваем первую страницу: с user= Twitch
        # возвращает либо одну запись, либо пустой список
        return bool(await followers.followers)
