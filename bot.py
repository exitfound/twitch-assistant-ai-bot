import asyncio
import contextlib
import logging
import signal

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from src.core import stream as twitch_stream
from src.core.chat_socket import SocketWatch, check_private_api, keep_migrated_sockets
from src.core.component import ChatComponent
from src.core.config import Emote, Help, Medoed, Memory, Proactive, Rewards, Roll, Twitch, validate_config
from src.core.content import Content, validate_content
from src.core.cooldowns import Cooldowns
from src.core.database import close_db, init_db
from src.core.logging_setup import setup_logging
from src.core.stream import StreamTracker, watch_stream
from src.core.tasks import BackgroundTasks
from src.core.tokens import (
    OAUTH_SCOPES, OAUTH_SCOPES_FOLLOWS, add_bot_token, add_broadcaster_token, oauth_link, store_tokens,
    token_dead, token_problem,
)
from src.core.utils import defuse
from src.gemini.memory import build as memory
from src.gemini.proactive import proactive_loop
from src.local.emote_spam import emote_spam_loop
from src.local import clip
from src.local.help_announce import help_loop
from src.local.medoed.feed import medoed_loop
from src.local.roll import perks
from src.local.roll.announce import curse_lift_loop
from src.local.roll.rewards import REWARDS_SCOPE, RewardComponent, RewardService

logger = logging.getLogger(__name__)

# Pause before the next attempt to start the rewards after a failure: doubles up to the cap
REWARDS_RETRY_SECONDS = 60
REWARDS_RETRY_MAX_SECONDS = 600


class Bot(commands.Bot):

    def __init__(self) -> None:
        super().__init__(
            client_id=Twitch.CLIENT_ID,
            client_secret=Twitch.CLIENT_SECRET,
            bot_id=Twitch.BOT_ID,
            prefix='!',
        )
        self._cooldowns = Cooldowns()
        self._bot_name: str | None = None
        self._channel_id: str | None = None
        self._tasks = BackgroundTasks()
        self._rewards = RewardService(self)
        self._broadcaster_token = False
        self._chat = SocketWatch(
            self, 'чат', lambda: str(self.bot_id), eventsub.ChatMessageSubscription.type,
            frozenset(sub.type for sub in (
                eventsub.ChannelFollowSubscription, eventsub.StreamOnlineSubscription,
                eventsub.StreamOfflineSubscription,
            )),
            self._subscribe_to_chat, active=lambda: not self._shutting_down and bool(self._channel_id),
        )
        self._rewards_watch = SocketWatch(
            self, 'награды', lambda: self._channel_id, eventsub.ChannelPointsRedeemAddSubscription.type,
            frozenset(), self._rewards.resubscribe,
            active=lambda: not self._shutting_down and self._rewards.active,
            on_failure=self._rewards_failed,
        )
        # The session whose chat has been told that the rewards are down
        self._rewards_down_told: str | None = None
        # Set by run_bot(): re-installed after the web adapter has taken the signals
        self.on_shutdown_signal = None
        self._shutting_down = False
        self.stream = StreamTracker(end_lookup=self._stream_end_from_vod)

    @property
    def session_id(self) -> str:
        """The stream while it is live, otherwise the current date. See src/core/stream.py."""
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
        return self._rewards.active

    # --- cooldowns (src/core/cooldowns.py) -----------------------------------

    def cooldown_remaining(self, user: str, scope: str) -> float:
        return self._cooldowns.remaining(user, scope)

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None:
        self._cooldowns.set(user, seconds, scope)

    def clear_cooldown(self, user: str, scope: str) -> None:
        self._cooldowns.clear(user, scope)

    async def process_commands(self, payload) -> None:
        """twitchio's built-in command parsing is switched off.

        Commands are parsed by the registry in src/core/component.py. Without this
        override every «!…» and every reward redemption logs CommandNotFound with a
        traceback. Component listeners receive events separately and are unaffected.
        """

    # --- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        check_private_api(self)
        keep_migrated_sockets()
        await init_db()
        await add_bot_token(self)
        users = await self.fetch_users(logins=[Twitch.CHANNEL])
        if users:
            self._channel_id = str(users[0].id)
            await self._add_broadcaster_token()
            await self._sync_stream()
        else:
            logger.error('Канал %s не найден – проактив и отправка в чат работать не будут', Twitch.CHANNEL)
        await self.add_component(ChatComponent(self))
        await self.add_component(RewardComponent(self._rewards))

    async def _add_broadcaster_token(self) -> None:
        """The channel's token for rewards (src/core/tokens.py)."""
        if Rewards.ENABLED:
            self._broadcaster_token = await add_broadcaster_token(self, self._channel_id, REWARDS_SCOPE)

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
        """Stream is live: stream session, rewards open, perks from the previous one."""
        await self.stream.online(stream_id, started_at)
        await self._rewards.set_open(True)
        await self.tell_rewards_down()
        await perks.on_stream_start(self, self.session_id)

    async def stream_went_offline(self) -> None:
        """Stream ended: session by date, game closed, rewards paused."""
        await self.stream.offline()
        await self._rewards.set_open(False)

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
                    'под аккаунтом бота:\n%s', oauth_link(OAUTH_SCOPES),
                )
        self._start_background_tasks()
        # The overlay feed needs no channel id: it only counts chat and serves the mood
        if Medoed.ENABLED:
            self._tasks.start('medoed', medoed_loop)
        # Memory of chat conversations, including those that ended while the bot was down
        self._start_memory()
        await self._start_rewards()
        await self.tell_rewards_down()
        if self.stream_live:
            # Perks from the previous stream. What was granted is not granted twice,
            # so a restart or reconnect mid-stream will not duplicate them
            await perks.on_stream_start(self, self.session_id)

    def _start_background_tasks(self) -> None:
        if not self._channel_id:
            if Proactive.ENABLED or Emote.SPAM_ENABLED or Rewards.ENABLED:
                logger.warning('ID канала не получен – фоновые задачи не запущены')
            return
        tasks = self._tasks
        tasks.start('chat_watch', self._chat.loop)
        if Rewards.ENABLED:
            tasks.start('rewards_watch', self._rewards_watch.loop)
        # The Twitch check catches a missed stream start or end
        tasks.start('stream_watch', lambda: watch_stream(self))
        if Proactive.ENABLED and tasks.start('proactive', lambda: proactive_loop(self)):
            logger.info('Проактивные сообщения включены (раз в %d–%d мин)',
                        Proactive.INTERVAL_MIN_MINUTES, Proactive.INTERVAL_MAX_MINUTES)
        # Curses come from rewards and from the previous stream's results
        if (Rewards.ENABLED or Roll.PERKS_ENABLED) and tasks.start('curse_lift', lambda: curse_lift_loop(self)):
            logger.info('Оповещения о снятии проклятий включены')
        if Help.ANNOUNCE_ENABLED and tasks.start('help', lambda: help_loop(self)):
            logger.info('Напоминания о командах включены (раз в %d мин)', Help.ANNOUNCE_INTERVAL_MINUTES)
        if Emote.SPAM_ENABLED and not tasks.running('emote_spam'):
            if Content.items('emotes'):
                tasks.start('emote_spam', lambda: emote_spam_loop(self))
                logger.info('Спам эмотами включён (раз в %d–%d мин)',
                            Emote.SPAM_INTERVAL_MIN_MINUTES, Emote.SPAM_INTERVAL_MAX_MINUTES)
            else:
                logger.warning(
                    'EMOTE_SPAM_ENABLED=true, но список эмотов в CONTENT.md пуст – '
                    'задача не запущена'
                )

    async def _start_rewards(self) -> None:
        # Also called once the channel's token arrives by OAuth – start() survives a second call
        if not Rewards.ENABLED or self._rewards.active or not self._channel_id:
            return
        if not self._broadcaster_token:
            logger.warning(
                'Награды за баллы канала выключены: нет токена канала. Открой в браузере '
                'и войди под аккаунтом канала %s:\n%s',
                Twitch.CHANNEL, oauth_link(REWARDS_SCOPE),
            )
            return
        try:
            await self._rewards.start(self._channel_id, open_=self.stream_live)
        except Exception as e:
            logger.exception('Награды за баллы канала не запущены')
            # A Twitch or network failure: without a retry the rewards stay dead until a
            # restart, and open on Twitch if the last run crashed before pausing them
            if not await self._rewards_failed(e):
                self._tasks.start('rewards_retry', self._retry_rewards, once=True)

    async def _retry_rewards(self) -> None:
        """Start the rewards again until it works, the channel's token dies or the bot stops."""
        delay = REWARDS_RETRY_SECONDS
        while True:
            logger.info('Награды за баллы канала: новая попытка через %d с', delay)
            await asyncio.sleep(delay)
            if self._shutting_down or self._rewards.active or not self._broadcaster_token:
                return
            try:
                await self._rewards.start(self._channel_id, open_=self.stream_live)
                return
            except Exception as e:
                if await self._rewards_failed(e):
                    return
                logger.warning('Награды за баллы канала снова не запустились: %r', e)
            delay = min(delay * 2, REWARDS_RETRY_MAX_SECONDS)

    async def _rewards_failed(self, error: Exception) -> bool:
        """The rewards watch could not resubscribe. True – the channel's token is dead and
        retrying stops until a new login."""
        if not token_dead(error):
            return False
        self._broadcaster_token = False
        self._rewards.drop()
        logger.error(
            'Twitch отозвал токен канала – награды за баллы выключены: %s\nОткрой в браузере '
            'и войди под аккаунтом канала %s:\n%s', error, Twitch.CHANNEL, oauth_link(REWARDS_SCOPE),
        )
        await self.tell_rewards_down()
        return True

    async def tell_rewards_down(self) -> None:
        """Tell the chat, once per stream, that the rewards are off for want of the channel's token.

        The rewards stay open on Twitch: without the token the bot can neither pause them
        nor see a redemption, and the points spent wait for the refund after a new login.
        """
        if not Rewards.ENABLED or self._broadcaster_token or not self._channel_id or not self.stream_live:
            return
        session = self.session_id
        if self._rewards_down_told == session:
            return
        self._rewards_down_told = session
        if not await self.send_chat_message(Content.text('rewards_down', channel=Twitch.CHANNEL)):
            self._rewards_down_told = None     # not heard: the next check says it again

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
            self._broadcaster_token = True
            await self._start_rewards()

    async def close(self, **options) -> None:
        # The chat watch must not resubscribe a bot that is shutting down
        self._shutting_down = True
        # The loops go first: they send to chat and call Helix through the HTTP session
        # that super().close() is about to close
        await self.stop_background_tasks()
        # Pause rewards before the HTTP session closes: without the bot a redemption
        # would take the points, and nobody would be there to apply it
        await self._rewards.stop()
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
        """Clip the stream's last seconds as the bot: the link, or None (src/local/clip.py)."""
        if not self._channel_id:
            raise RuntimeError('ID канала не получен – клип сделать нельзя')
        return await clip.make_clip(self, self._channel_id, str(self.bot_id), seconds, title)

    async def stop_background_tasks(self) -> None:
        await self._tasks.stop()

    async def event_websocket_closed(self, payload) -> None:
        await asyncio.gather(self._chat.closed(), self._rewards_watch.closed())

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
                           e, oauth_link(OAUTH_SCOPES_FOLLOWS))

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


async def run_bot() -> None:
    setup_logging('INFO')
    validate_config()
    validate_content()
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(name: str) -> None:
        # Logged here: otherwise a stop on a signal is indistinguishable from a
        # process that stopped for its own reasons
        logger.info('Получен %s, завершаюсь...', name)
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _on_signal, 'SIGTERM')
    loop.add_signal_handler(signal.SIGINT, _on_signal, 'SIGINT')
    try:
        async with Bot() as bot:
            bot.on_shutdown_signal = _on_signal
            bot_task = asyncio.create_task(bot.start())
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            done, _ = await asyncio.wait(
                [bot_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot_task
            else:
                # The bot stopped on its own. Its exception has to be taken out of the
                # task, otherwise the process exits as if nothing happened and the
                # traceback surfaces only as «Task exception was never retrieved»
                shutdown_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception('Бот остановился из-за ошибки')
            await bot.stop_background_tasks()
    finally:
        await close_db()


if __name__ == '__main__':
    from src.cli.main import main as cli_main
    if not cli_main():
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run_bot())
