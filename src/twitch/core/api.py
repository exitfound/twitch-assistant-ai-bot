"""The bot's internal API: what the other services ask of Twitch through the bot.

The bot holds every Twitch token, so a service sends its chat lines, follow checks and
chat-list requests here. Served on BOT_API_HOST:BOT_API_PORT, on the compose network only.
"""
import contextlib
import logging
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from aiohttp import web

from src.core.config import Service
from src.core.service import read_json, require_token
from src.twitch.core.followers import FollowerCache
from src.twitch.core.limits import deny
from src.twitch.core.replies import reply

if TYPE_CHECKING:
    import twitchio

    from src.twitch.core.port import BotPort

logger = logging.getLogger(__name__)


class BotApi:

    def __init__(self, bot: 'BotPort', followers: FollowerCache) -> None:
        self._bot = bot
        self._followers = followers
        # Chat messages a service is answering right now, by id: a reply needs the message
        self._held: dict[str, twitchio.ChatMessage] = {}

    @contextlib.contextmanager
    def hold(self, message: 'twitchio.ChatMessage') -> Iterator[None]:
        """Keep the message answerable by id while a service handles it."""
        self._held[message.id] = message
        try:
            yield
        finally:
            self._held.pop(message.id, None)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[require_token(Service.TOKEN)])
        app.router.add_get('/state', self._state)
        app.router.add_post('/chat', self._chat)
        app.router.add_post('/reply', self._reply)
        app.router.add_get('/follows', self._follows)
        app.router.add_get('/chatters', self._chatters)
        return app

    async def _state(self, _request: web.Request) -> web.Response:
        bot = self._bot
        return web.json_response({'session_id': bot.session_id, 'live': bot.stream_live,
                                  'rewards_active': bot.rewards_active, 'at': time.time()})

    async def _chat(self, request: web.Request) -> web.Response:
        body = await read_json(request, 'text')
        return web.json_response({'sent': await self._bot.send_chat_message(str(body['text']))})

    async def _reply(self, request: web.Request) -> web.Response:
        body = await read_json(request, 'message_id', 'text')
        message = self._held.get(str(body['message_id']))
        if message is None:
            # The command's call has ended: there is nothing to reply to any more
            logger.warning('Ответ на сообщение %s опоздал и не ушёл', body['message_id'])
            return web.json_response({'sent': False})
        if body.get('deny'):
            # A refusal by a limit: the same once-per-30-s brake as the bot's own refusals
            await deny(self._bot, message, message.chatter.name, str(body['text']))
            return web.json_response({'sent': True})
        return web.json_response({'sent': await reply(message, str(body['text']))})

    async def _follows(self, request: web.Request) -> web.Response:
        user_id = request.query.get('user_id', '')
        return web.json_response({'follows': await self._followers.is_follower(self._bot, user_id)})

    async def _chatters(self, _request: web.Request) -> web.Response:
        chatters = await self._bot.fetch_chatters()
        return web.json_response({'chatters': sorted(chatters) if chatters is not None else None})
