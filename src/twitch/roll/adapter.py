"""The game service as the bot hosts it: the link, the rewards' Twitch half, the stream hooks.

The bot knows only that a game is attached and calls these hooks; everything the bot does
on Twitch for the game is here. Present only with ROLL_URL.
"""
import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from twitchio import eventsub

from src.core.config import Rewards, Service, Twitch
from src.core.content import Content
from src.core.database import get_state, set_state
from src.core.service import serve
from src.core.tasks import BackgroundTasks
from src.twitch.core.api import BotApi
from src.twitch.core.chat_socket import SocketWatch
from src.twitch.core.followers import FollowerCache
from src.twitch.core.tokens import CHANNEL_LOGIN, add_broadcaster_token, oauth_link, token_dead
from src.twitch.roll.client import GameCommand, RollClient, RollUnavailable
from src.twitch.roll.rewards import REWARDS_SCOPE, RewardComponent, RewardService, get_reward_ids

if TYPE_CHECKING:
    from src.twitch.bot import Bot
    from src.twitch.core.component import ChatComponent

logger = logging.getLogger(__name__)

# Pause before the next attempt to start the rewards after a failure: doubles up to the cap
REWARDS_RETRY_SECONDS = 60
REWARDS_RETRY_MAX_SECONDS = 600
# The game's commands as it last declared them: a bot starting while the game is down
# still knows them and answers «unavailable» instead of nothing
COMMANDS_STATE = 'roll_commands'
COMMANDS_RETRY_SECONDS = 30


class RollAdapter:

    def __init__(self, bot: 'Bot', followers: FollowerCache, tasks: BackgroundTasks,
                 stopping: Callable[[], bool]) -> None:
        self._bot = bot
        self._tasks = tasks
        self._stopping = stopping
        self.api = BotApi(bot, followers)
        self.client = RollClient(bot, self.api)
        self.rewards = RewardService(bot, self.client)
        self.broadcaster_token = False
        self._chat: ChatComponent | None = None
        # The session whose chat has been told that the rewards are down
        self._down_told: str | None = None
        self.rewards_watch = SocketWatch(
            bot, 'награды', lambda: bot.channel_id, eventsub.ChannelPointsRedeemAddSubscription.type,
            frozenset(), self.rewards.resubscribe,
            active=lambda: not stopping() and self.rewards.active,
            on_failure=self._rewards_failed,
        )

    @property
    def rewards_enabled(self) -> bool:
        return Rewards.ENABLED

    # --- start --------------------------------------------------------------

    async def add_broadcaster_token(self) -> None:
        """The channel's token for rewards (src/twitch/core/tokens.py)."""
        if self.rewards_enabled:
            self.broadcaster_token = await add_broadcaster_token(self._bot, self._bot.channel_id, REWARDS_SCOPE)

    async def attach(self, chat: 'ChatComponent') -> None:
        """Hook into the bot: the redemption listener, and the game's commands as last declared."""
        self._chat = chat
        await self._bot.add_component(RewardComponent(self.rewards))
        try:
            cached = await get_state(COMMANDS_STATE)
            if cached:
                chat.set_game_commands([GameCommand(**c) for c in json.loads(cached)])
        except Exception:
            logger.exception('Команды игры из прошлого запуска не прочитаны')

    def start_services(self) -> None:
        """The bot's API for the game, the game's event queue and its commands. No channel id needed."""
        self._tasks.start('bot_api', lambda: serve(self.api.app(), Service.BOT_API_HOST, Service.BOT_API_PORT,
                                                   'API бота для служб'))
        self._tasks.start('roll_events', self.client.events_loop)
        self._tasks.start('roll_commands', self._load_commands, once=True)

    def start_watch(self) -> None:
        """The rewards watch: needs the channel id."""
        if self.rewards_enabled:
            self._tasks.start('rewards_watch', self.rewards_watch.loop)

    async def _load_commands(self) -> None:
        """Ask the game for its commands until it answers; keep them for the next start."""
        while True:
            try:
                commands = await self.client.commands()
            except RollUnavailable as e:
                logger.info('Команды игры пока не получены (%s) – новая попытка через %d с', e, COMMANDS_RETRY_SECONDS)
                await asyncio.sleep(COMMANDS_RETRY_SECONDS)
                continue
            if self._chat is not None:
                self._chat.set_game_commands(commands)
            logger.info('Команды игры: %s', ', '.join(c.trigger for c in commands))
            try:
                await set_state(COMMANDS_STATE, json.dumps([c.as_dict() for c in commands]))
            except Exception:
                logger.exception('Команды игры не сохранены')
            return

    async def on_ready(self) -> None:
        bot = self._bot
        await self.start_rewards()
        await self.tell_rewards_down()
        if not (bot.stream_live and self.rewards.active):
            # The game's titles, regalia and perks. Rewards that came up mid-stream told the
            # game already (_rewards_started()); without them only the Сглаз goes out, and what
            # was granted is not granted twice, so a restart mid-stream duplicates nothing
            await self.client.stream(bot.session_id)

    # --- stream -------------------------------------------------------------

    async def on_online(self) -> None:
        """The stream started: rewards open, the game told (old curses, perks)."""
        await self.rewards.set_open(True)
        await self.tell_rewards_down()
        await self.client.stream(self._bot.session_id)

    async def on_offline(self, ended: str) -> None:
        """The stream ended: rewards paused, the game told which stream it was."""
        await self.rewards.set_open(False)
        await self.client.stream(ended)

    # --- rewards ------------------------------------------------------------

    async def on_channel_login(self) -> None:
        """A new login as the channel: the rewards come up with its token."""
        self.broadcaster_token = True
        await self.start_rewards()

    async def start_rewards(self) -> None:
        # Also called once the channel's token arrives by OAuth – start() survives a second call
        bot = self._bot
        if not self.rewards_enabled or self.rewards.active or not bot.channel_id:
            return
        if not self.broadcaster_token:
            logger.warning(
                'Награды за баллы канала выключены: нет токена канала. Открой в браузере '
                'и войди под аккаунтом канала %s:\n%s',
                Twitch.CHANNEL, oauth_link(CHANNEL_LOGIN),
            )
            return
        try:
            await self.rewards.start(bot.channel_id, open_=bot.stream_live)
        except Exception as e:
            logger.exception('Награды за баллы канала не запущены')
            # A Twitch, network or game failure: without a retry the rewards stay dead until
            # a restart, and open on Twitch if the last run crashed before pausing them
            if not await self._rewards_failed(e):
                self._tasks.start('rewards_retry', self._retry_rewards, once=True)
            return
        await self._rewards_started()

    async def _rewards_started(self) -> None:
        """The rewards came up. Mid-stream the champion's Благодать, held back while
        they were down, is granted now; the game grants nothing twice."""
        if self._bot.stream_live:
            await self.client.stream(self._bot.session_id)

    async def _retry_rewards(self) -> None:
        """Start the rewards again until it works, the channel's token dies or the bot stops."""
        delay = REWARDS_RETRY_SECONDS
        while True:
            logger.info('Награды за баллы канала: новая попытка через %d с', delay)
            await asyncio.sleep(delay)
            if self._stopping() or self.rewards.active or not self.broadcaster_token:
                return
            try:
                await self.rewards.start(self._bot.channel_id, open_=self._bot.stream_live)
            except Exception as e:
                if await self._rewards_failed(e):
                    return
                logger.warning('Награды за баллы канала снова не запустились: %r', e)
            else:
                await self._rewards_started()
                return
            delay = min(delay * 2, REWARDS_RETRY_MAX_SECONDS)

    async def _rewards_failed(self, error: Exception) -> bool:
        """The rewards watch could not resubscribe. True – the channel's token is dead and
        retrying stops until a new login."""
        if not token_dead(error):
            return False
        self.broadcaster_token = False
        self.rewards.drop()
        logger.error(
            'Twitch отозвал токен канала – награды за баллы выключены: %s\nОткрой в браузере '
            'и войди под аккаунтом канала %s:\n%s', error, Twitch.CHANNEL, oauth_link(CHANNEL_LOGIN),
        )
        await self.tell_rewards_down()
        return True

    async def tell_rewards_down(self) -> None:
        """Tell the chat, once per stream, that the rewards are off for want of the channel's token.

        The rewards stay open on Twitch: without the token the bot can neither pause them
        nor see a redemption, and the points spent wait for the refund after a new login.
        A channel that never gave the token has no rewards of the bot to warn about.
        """
        bot = self._bot
        if not self.rewards_enabled or self.broadcaster_token or not bot.channel_id or not bot.stream_live:
            return
        session = bot.session_id
        if self._down_told == session:
            return
        self._down_told = session
        try:
            created = bool(await get_reward_ids())
        except Exception:
            logger.exception('Не удалось проверить, созданы ли награды')
            created = True          # unsure: warning for nothing beats points spent for nothing
        if not created:
            return
        if not await bot.send_chat_message(Content.text('rewards_down', channel=Twitch.CHANNEL)):
            self._down_told = None     # not heard: the next check says it again

    # --- shutdown -----------------------------------------------------------

    async def close(self) -> None:
        """Pause the rewards and close the link. Before twitchio closes its HTTP session:
        without the bot a redemption would take the points, and nobody would apply it."""
        await self.rewards.stop()
        await self.client.close()

    async def socket_closed(self) -> None:
        await self.rewards_watch.closed()
