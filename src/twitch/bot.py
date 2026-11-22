import asyncio
import logging
import signal

import twitchio
from aiohttp import web
from twitchio import eventsub
from twitchio.ext import commands

from src.twitch.core import stream as twitch_stream
from src.twitch.core.chat_socket import SocketWatch, check_private_api, keep_migrated_sockets
from src.core import sse
from src.twitch.core.component import ChatComponent
from src.twitch.core.followers import FollowerCache
from src.core.config import Emote, Film, Help, Mascot, Memory, Proactive, Pulse, Service, Twitch
from src.core import speech
from src.core.content import Content
from src.core.cooldowns import Cooldowns
from src.twitch.core.stream import StreamTracker, watch_stream
from src.core.tasks import BackgroundTasks
from src.twitch.core.tokens import (
    BOT_LOGIN, add_bot_token, add_login_routes, oauth_link, store_tokens,
    token_problem,
)
from src.core.utils import defuse
from src.gemini.memory import build as memory
from src.twitch.gemini.proactive import proactive_loop
from src.twitch.gemini.pulse import pulse_loop
from src.twitch.local.emote_spam import emote_spam_loop
from src.twitch.local import clip, film, film_bridge
from src.twitch.local import schedule
from src.twitch.local.help_announce import help_loop
from src.twitch.local.mascot import feed as mascot_feed
from src.twitch.local.mascot.feed import mascot_loop
from src.twitch.roll.adapter import RollAdapter
from src.twitch.roll.rewards import REWARDS_SCOPE

logger = logging.getLogger(__name__)

# How long a mass flask waits for Twitch's chat list before it takes who has written instead
CHATTERS_TIMEOUT = 5


# The cinema hall's session left open at the stream end, closed after the resume window
FILM_CLOSE_TASK = 'film_close'

class Bot(commands.Bot):

    def __init__(self) -> None:
        super().__init__(
            client_id=Twitch.CLIENT_ID,
            client_secret=Twitch.CLIENT_SECRET,
            bot_id=Twitch.BOT_ID,
            prefix='!',
        )
        # /bot and /channel beside twitchio's /oauth: links a terminal cannot wrap and cut short
        add_login_routes(self._adapter, REWARDS_SCOPE)
        self._cooldowns = Cooldowns()
        self._bot_name: str | None = None
        self._channel_id: str | None = None
        self._tasks = BackgroundTasks()
        self._followers = FollowerCache()
        # The game service (src/roll/) with everything the bot does on Twitch for it;
        # without ROLL_URL the game is off: no game commands, no rewards
        self._game = (RollAdapter(self, self._followers, self._tasks, lambda: self._shutting_down)
                      if Service.ROLL_URL else None)
        self._chat = SocketWatch(
            self, 'чат', lambda: str(self.bot_id), eventsub.ChatMessageSubscription.type,
            frozenset(sub.type for sub in (
                eventsub.ChannelFollowSubscription, eventsub.StreamOnlineSubscription,
                eventsub.StreamOfflineSubscription,
            )),
            self._subscribe_to_chat, active=lambda: not self._shutting_down and bool(self._channel_id),
        )
        # Set by run_bot(): re-installed after the web adapter has taken the signals
        self.on_shutdown_signal = None
        self._shutting_down = False
        self.stream = StreamTracker(end_lookup=self._stream_end_from_vod)

    @property
    def session_id(self) -> str:
        """The stream while it is live, otherwise the current date. See src/twitch/core/stream.py."""
        return self.stream.session_id

    @property
    def stream_live(self) -> bool:
        return self.stream.live

    @property
    def bot_name(self) -> str | None:
        return self._bot_name

    @property
    def channel_id(self) -> str | None:
        return self._channel_id

    @property
    def rewards_active(self) -> bool:
        return self._game is not None and self._game.rewards.active

    # --- cooldowns (src/core/cooldowns.py) -----------------------------------

    def cooldown_remaining(self, user: str, scope: str) -> float:
        return self._cooldowns.remaining(user, scope)

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None:
        self._cooldowns.set(user, seconds, scope)

    def clear_cooldown(self, user: str, scope: str) -> None:
        self._cooldowns.clear(user, scope)

    async def process_commands(self, payload) -> None:
        """twitchio's built-in command parsing is switched off.

        Commands are parsed by the registry in src/twitch/core/component.py. Without this
        override every «!…» and every reward redemption logs CommandNotFound with a
        traceback. Component listeners receive events separately and are unaffected.
        """

    # --- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        check_private_api(self)
        keep_migrated_sockets()
        await add_bot_token(self)
        users = await self.fetch_users(logins=[Twitch.CHANNEL])
        if users:
            self._channel_id = str(users[0].id)
            if self._game is not None:
                await self._game.add_broadcaster_token()
            await self._sync_stream()
        else:
            logger.error('Канал %s не найден – проактив и отправка в чат работать не будут', Twitch.CHANNEL)
        chat = ChatComponent(self, self._followers, self._game.client if self._game else None)
        await self.add_component(chat)
        if self._game is not None:
            await self._game.attach(chat)

    # --- stream -----------------------------------------------------------

    async def fetch_live_stream(self) -> tuple[str, float] | None:
        """The channel's live stream according to Twitch: (id, start) or None. Does not swallow errors."""
        return await twitch_stream.fetch_live_stream(self, self._channel_id)

    async def _sync_stream(self) -> None:
        """Whether the stream is live right now.

        Events about what happened while the bot was down will never arrive: the stream
        may have started or ended without it. On a request error the stream still open in
        the DB is taken as live – a restart mid-stream must not split the session or close
        the game – and the watch_stream() check corrects that within a few minutes.
        """
        try:
            live = await self.fetch_live_stream()
        except Exception:
            logger.warning('Не удалось узнать, идёт ли эфир', exc_info=True)
            await self.stream.resume_open()
            return
        if live:
            await self.stream.online(*live)
        else:
            await self.stream.settle_missed_end()

    async def stream_went_online(self, stream_id: str, started_at: float) -> None:
        """Stream is live: stream session, rewards open, old curses gone, perks."""
        await self.stream.online(stream_id, started_at)
        # Back within the resume window: the same stream, the cinema hall stays open
        self._tasks.cancel(FILM_CLOSE_TASK)
        if self._game is not None:
            await self._game.on_online()

    async def stream_went_offline(self) -> None:
        """Stream ended: session by date, game closed, rewards paused."""
        # The overlay keeps the titles of the stream that ended, not of the date session after it
        ended = self.session_id
        # A rerun or a premiere ending, or a redelivered offline, ends no stream of ours
        was_live = self.stream.stream_id is not None
        # Before the steps below: one of them failing must not leave the hall open
        if Film.ENABLED and was_live:
            self._tasks.start(FILM_CLOSE_TASK, lambda: film.close_after_stream(self), once=True)
        await self.stream.offline()
        if self._game is not None:
            await self._game.on_offline(ended)

    def _start_memory(self) -> None:
        """The memory check (src/gemini/memory/): runs for as long as the bot does.

        It goes by silence in chat, not by the stream state, so a stream end it
        missed or one that came while the bot was down changes nothing.
        """
        if Memory.ENABLED:
            self._tasks.start('memory', memory.memory_loop)

    async def _stream_end_from_vod(self, stream_id: str, started_at: float) -> float | None:
        return await twitch_stream.end_from_vod(self, self._channel_id, started_at)

    async def event_stream_online(self, payload) -> None:
        # A rerun or a premiere is not a stream with the streamer
        if payload.type != 'live':
            return
        try:
            await self.stream_went_online(payload.id, payload.started_at.timestamp())
        except Exception:
            logger.exception('Начало эфира не обработано')

    async def event_stream_offline(self, payload) -> None:
        try:
            await self.stream_went_offline()
        except Exception:
            logger.exception('Конец эфира не обработан')

    def install_signal_handlers(self) -> None:
        """Take SIGTERM and SIGINT back from aiohttp.

        twitchio's OAuth adapter runs as web.AppRunner(..., handle_signals=True) and
        aiohttp replaces any handlers installed before start(). Without reinstalling
        them a signal skips the orderly shutdown for asyncio.run()'s emergency cleanup,
        and nothing logs that it arrived.
        """
        if self.on_shutdown_signal is None:
            return
        loop = asyncio.get_running_loop()
        for sig, name in ((signal.SIGTERM, 'SIGTERM'), (signal.SIGINT, 'SIGINT')):
            loop.add_signal_handler(sig, self.on_shutdown_signal, name)

    async def event_ready(self) -> None:
        self.install_signal_handlers()
        # twitchio fetched the bot's user at login, failing the start if it could not.
        # ready fires once per start: an error escaping here would leave the bot deaf
        self._bot_name = getattr(self.user, 'name', None)
        if not self._bot_name:
            try:
                users = await self.fetch_users(ids=[self.bot_id])
                self._bot_name = users[0].name if users else None
            except Exception:
                logger.exception('Имя бота не получено – бот не ответит в чате до перезапуска')
        logger.info('Бот запущен | Ник: %s | Сессия: %s', self._bot_name or self.bot_id, self.session_id)
        try:
            await self._subscribe_to_chat()
        except Exception as e:
            logger.warning('Не удалось подписаться на чат: %r', e)
            if token_problem(e):
                logger.warning(
                    'Похоже, нет токена бота или у него не те права. Открой в браузере и войди '
                    'под аккаунтом бота:\n%s', oauth_link(BOT_LOGIN),
                )
        self._start_background_tasks()
        self._start_overlays()
        # Memory of chat conversations, including those that ended while the bot was down
        self._start_memory()
        if self._game is not None:
            # Rewards, the warning about a dead channel token, and the stream state to the game
            await self._game.on_ready()

    def _start_overlays(self) -> None:
        """The mascot's OBS page and recount, the API of the services and the game's event queue.
        None of them needs the channel id; the game serves its own overlay."""
        if Mascot.ENABLED:
            app = web.Application()
            mascot_feed.add_routes(app)
            self._tasks.start('mascot', mascot_loop)
            self._tasks.start('overlays', lambda: sse.serve(app, [mascot_feed.feed], Mascot.HOST, Mascot.PORT))
        if self._game is not None:
            self._game.start_services()
        if Service.VOICE_URL and self._tasks.start('voice', speech.Forwarder(Service.VOICE_URL, Service.TOKEN).loop):
            logger.info('Реплики бота уходят на озвучку: %s', Service.VOICE_URL)

    def _start_background_tasks(self) -> None:
        # The !film commands work without the channel id, so the session they open needs
        # its heartbeat too, or the gate ends it after GATE_LEASE_SECONDS
        if Film.ENABLED:
            self._tasks.start('film_heartbeat', lambda: film.heartbeat_loop(self))
        if Film.BRIDGE and self._tasks.start('film_bridge', film_bridge.bridge_loop):
            logger.info('Мост чата Twitch в кинозал включён (пока открыт сеанс)')
        if not self._channel_id:
            if Proactive.ENABLED or Emote.SPAM_ENABLED or (self._game is not None and self._game.rewards_enabled):
                logger.warning('ID канала не получен – фоновые задачи не запущены')
            return
        tasks = self._tasks
        tasks.start('chat_watch', self._chat.loop)
        if self._game is not None:
            self._game.start_watch()
        # The Twitch check catches a missed stream start or end
        tasks.start('stream_watch', lambda: watch_stream(self))
        loops = (
            (Proactive.ENABLED, 'proactive', lambda: proactive_loop(self),
             ('Проактивные сообщения включены (раз в %d–%d мин)',
              Proactive.INTERVAL_MIN_MINUTES, Proactive.INTERVAL_MAX_MINUTES)),
            (Pulse.ENABLED, 'pulse', lambda: pulse_loop(self),
             ('Пульс чата включён (раз в %d мин во время эфира, пока пишут)', Pulse.INTERVAL_MINUTES)),
            (Help.ANNOUNCE_ENABLED, 'help', lambda: help_loop(self),
             ('Напоминания о командах включены (раз в %d мин)', Help.ANNOUNCE_INTERVAL_MINUTES)),
        )
        for enabled, name, loop, announce in loops:
            if enabled and tasks.start(name, loop):
                logger.info(*announce)
        if Emote.SPAM_ENABLED:
            self._start_emote_spam()

    def _start_emote_spam(self) -> None:
        if self._tasks.running('emote_spam'):
            return
        if not Content.items('emotes'):
            logger.warning('EMOTE_SPAM_ENABLED=true, но список эмотов в CONTENT.md пуст – задача не запущена')
            return
        self._tasks.start('emote_spam', lambda: emote_spam_loop(self))
        logger.info('Спам эмотами включён (раз в %d–%d мин)',
                    Emote.SPAM_INTERVAL_MIN_MINUTES, Emote.SPAM_INTERVAL_MAX_MINUTES)

    async def event_subscription_revoked(self, payload) -> None:
        # Twitch says why: authorization_revoked, user_removed, … The watch handles the rest
        logger.warning('Twitch отозвал подписку %s: %s', payload.type, payload.status)

    async def event_oauth_authorized(self, payload: twitchio.authentication.UserTokenPayload):
        await self.add_token(payload.access_token, payload.refresh_token)
        if str(payload.user_id) == str(self.bot_id):
            await store_tokens(self, 'бота', 'TWITCH_BOT_TOKEN', payload)
            if self._channel_id:
                await self._subscribe_to_chat()
        elif self._channel_id and str(payload.user_id) == self._channel_id:
            await store_tokens(self, 'канала', 'TWITCH_BROADCASTER_TOKEN', payload)
            if self._game is not None:
                await self._game.on_channel_login()

    async def close(self, **options) -> None:
        # close() runs more than once on shutdown; the hall is closed by the first one
        first = not self._shutting_down
        # The chat watch must not resubscribe a bot that is shutting down
        self._shutting_down = True
        # The loops go first: they send to chat and call Helix through the HTTP session
        # that super().close() is about to close
        await self.stop_background_tasks()
        # Pause rewards before the HTTP session closes: without the bot a redemption
        # would take the points, and nobody would be there to apply it. Before the hall:
        # a gate that does not answer takes up to 4 s of the container's 10
        if self._game is not None:
            await self._game.close()
        # The cinema hall lives while the bot does; the chat line needs the HTTP session too
        if Film.ENABLED and first:
            await film.close_on_shutdown(self)
        await super().close(**options)

    async def send_chat_message(self, text: str) -> bool:
        """Send without a reply (HTTP API). True if the chat got it.

        Every self-initiated line goes through here – proactive remarks, the second and
        later chunks of an answer, emotes, announcements – so this is the single place
        where a line that would read as a chat command is defused. Twitch answers 200
        for a line it then drops (AutoMod, a duplicate within 30 s) and says so in
        is_sent: such a line is not counted, charged or saved as said.
        """
        if not self._channel_id:
            logger.warning('Отправка невозможна: ID канала не получен')
            return False
        text = defuse(text)
        if not text:
            return False
        try:
            response = await self._http.post_chat_message(
                broadcaster_id=self._channel_id,
                sender_id=str(self.bot_id),
                message=text,
                token_for=str(self.bot_id),
            )
        except Exception:
            logger.exception('Не удалось отправить сообщение в чат')
            return False
        result = ((response or {}).get('data') or [{}])[0]
        if result.get('is_sent', True) is False:
            reason = result.get('drop_reason') or {}
            logger.warning('Twitch отбросил сообщение (%s): %r', reason.get('code'), text[:80])
            return False
        return True

    async def create_clip(self, seconds: int, title: str | None) -> str | None:
        """Clip the stream's last seconds as the bot: the link, or None (src/twitch/local/clip.py)."""
        if not self._channel_id:
            raise RuntimeError('ID канала не получен – клип сделать нельзя')
        return await clip.make_clip(self, self._channel_id, str(self.bot_id), seconds, title)

    async def fetch_chatters(self) -> set[str] | None:
        """Logins connected to the channel's chat, the bot's own left out. None – Twitch did not say.

        Asked as the bot, a moderator of the channel (moderator:read:chatters). Never raises: a
        mass flask then takes who has written in the stream.
        """
        if not self._channel_id:
            return None
        try:
            async with asyncio.timeout(CHATTERS_TIMEOUT):
                chatters = await self.create_partialuser(self._channel_id).fetch_chatters(
                    moderator=str(self.bot_id), first=1000,
                )
                return {user.name.lower() async for user in chatters.users
                        if user.name and str(user.id) != str(self.bot_id)}
        except Exception as e:
            logger.warning('Список зрителей чата не получен, фласка возьмёт писавших в стриме: %r', e)
            if isinstance(e, twitchio.HTTPException) and e.status in (401, 403):
                logger.warning('Боту нужно право moderator:read:chatters и модерка в канале. Открой в '
                               'браузере и войди под аккаунтом бота:\n%s', oauth_link(BOT_LOGIN))
            return None

    async def fetch_schedule(self) -> dict | None:
        """The channel's schedule on Twitch (src/twitch/local/schedule.py); None – Twitch did not say."""
        if not self._channel_id:
            return None
        return await schedule.fetch(self._http, self._channel_id)

    async def stop_background_tasks(self) -> None:
        await self._tasks.stop()

    async def event_websocket_closed(self, payload) -> None:
        await asyncio.gather(self._chat.closed(), *([self._game.socket_closed()] if self._game else []))

    async def _subscribe_to_chat(self) -> None:
        if not self._channel_id:
            logger.error('ID канала не получен, подписка невозможна')
            return
        sub = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._channel_id,
            user_id=str(self.bot_id),
        )
        await self.subscribe_websocket(sub, as_bot=True)
        logger.info('Подписка на чат #%s', Twitch.CHANNEL)

        try:
            follow_sub = eventsub.ChannelFollowSubscription(
                broadcaster_user_id=self._channel_id,
                moderator_user_id=str(self.bot_id),
            )
            await self.subscribe_websocket(follow_sub, as_bot=True)
            logger.info('Подписка на фоловы')
        except Exception as e:
            logger.warning('Не удалось подписаться на фоловы: %s\nПереавторизация: %s',
                           e, oauth_link(BOT_LOGIN))

        try:
            for stream_sub in (
                eventsub.StreamOnlineSubscription(broadcaster_user_id=self._channel_id),
                eventsub.StreamOfflineSubscription(broadcaster_user_id=self._channel_id),
            ):
                await self.subscribe_websocket(stream_sub, as_bot=True)
            logger.info('Подписка на начало и конец эфира')
        except Exception as e:
            logger.warning(
                'Не удалось подписаться на начало и конец эфира: %s – '
                'сессия переключится только при перезапуске бота', e,
            )
