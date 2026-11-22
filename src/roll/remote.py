"""The bot as the game service sees it: RollPort over the bot's internal API.

The stream state is the one the bot sent last: every request from the bot carries it, and
the service asks for it at start. Until the bot answers, the game takes the stream as
offline, as the bot itself does before it has looked.
"""
import logging
from urllib.parse import quote

from src.core.cooldowns import Cooldowns
from src.core.service import ServiceClient, ServiceUnavailable
from src.core.utils import local_time

logger = logging.getLogger(__name__)

# The mass flask waits for Twitch's chat list up to 5 s on the bot's side
CHATTERS_SECONDS = 8


class RemoteBot:

    def __init__(self, client: ServiceClient) -> None:
        self._client = client
        # The game's own pauses: the spam pause of !roll and the once-per-30-s refusals
        self._cooldowns = Cooldowns()
        self._session: str | None = None
        self.stream_live = False
        self.rewards_active = False

    @property
    def session_id(self) -> str:
        return self._session or local_time().strftime('%Y-%m-%d')

    def update(self, state: dict) -> None:
        """The stream state as the bot sent it."""
        self._session = str(state['session_id'])
        self.stream_live = bool(state['live'])
        self.rewards_active = bool(state['rewards_active'])

    async def sync(self) -> bool:
        """Ask the bot for the stream state. False – the bot did not answer."""
        try:
            self.update(await self._client.call('GET', '/state'))
        except (ServiceUnavailable, KeyError) as e:
            logger.info('Бот не ответил на запрос состояния: %s', e)
            return False
        return True

    async def send_chat_message(self, text: str) -> bool:
        try:
            return bool((await self._client.call('POST', '/chat', {'text': text}))['sent'])
        except (ServiceUnavailable, KeyError) as e:
            logger.warning('Реплика игры не ушла в чат: %s', e)
            return False

    async def reply(self, message_id: str, text: str) -> bool:
        try:
            return bool((await self._client.call('POST', '/reply', {'message_id': message_id, 'text': text}))['sent'])
        except (ServiceUnavailable, KeyError) as e:
            logger.warning('Ответ игры не ушёл в чат: %s', e)
            return False

    async def fetch_chatters(self) -> set[str] | None:
        try:
            chatters = (await self._client.call('GET', '/chatters', seconds=CHATTERS_SECONDS))['chatters']
        except (ServiceUnavailable, KeyError) as e:
            logger.warning('Список чата для фласки не получен: %s', e)
            return None
        return set(chatters) if chatters is not None else None

    async def follows(self, user_id: str) -> bool:
        """Whether the buyer follows. Fails open, as the bot's follower cache does."""
        try:
            path = '/follows?user_id=' + quote(str(user_id))
            return bool((await self._client.call('GET', path))['follows'])
        except (ServiceUnavailable, KeyError) as e:
            logger.warning('Не удалось узнать, фолловер ли %s – считаю, что да: %s', user_id, e)
            return True

    def cooldown_remaining(self, user: str, scope: str) -> float:
        return self._cooldowns.remaining(user, scope)

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None:
        self._cooldowns.set(user, seconds, scope)
