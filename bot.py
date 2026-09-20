import asyncio
import logging
import os
import re
import signal
import time
from pathlib import Path

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from src.core.component import ChatComponent
from src.core.config import Emote, Help, Memory, Proactive, Rewards, Roll, Twitch, validate_config
from src.core.content import Content, validate_content
from src.core.database import close_db, init_db
from src.core.logging_setup import setup_logging
from src.core.stream import StreamTracker, watch_stream
from src.core.utils import defuse
from src.gemini.memory import build as memory
from src.gemini.proactive import proactive_loop
from src.local.emote_spam import emote_spam_loop
from src.local.help_announce import help_loop
from src.local.roll import perks
from src.local.roll.announce import curse_lift_loop
from src.local.roll.rewards import REWARDS_SCOPE, RewardComponent, RewardService

logger = logging.getLogger(__name__)

OAUTH_SCOPES = 'user:read:chat+user:write:chat+user:bot'
OAUTH_SCOPES_FOLLOWS = f'{OAUTH_SCOPES}+moderator:read:followers'

# A stream's recording is recognised by its start time: a VOD starts when the stream does
VOD_MATCH_SECONDS = 300

# How often the chat connection is checked (_watch_chat_socket)
CHAT_WATCH_SECONDS = 60

# Liveness file for the container healthcheck. Empty – no heartbeat, which is how
# a run outside a container behaves
HEARTBEAT_PATH = os.getenv('BOT_HEARTBEAT', '')
HEARTBEAT_SECONDS = 60

# Where twitchio keeps the tokens: the name it uses by default, next to the working
# directory. In the container that is the /data volume
TOKENS_FILE = '.tio.tokens.json'


class Bot(commands.Bot):

    def __init__(self) -> None:
        super().__init__(
            client_id=Twitch.CLIENT_ID,
            client_secret=Twitch.CLIENT_SECRET,
            bot_id=Twitch.BOT_ID,
            prefix='!',
        )
        self._cooldowns: dict[str, float] = {}
        self._bot_name: str | None = None
        self._channel_id: str | None = None
        self._proactive_task: asyncio.Task | None = None
        self._emote_spam_task: asyncio.Task | None = None
        self._help_task: asyncio.Task | None = None
        self._curse_lift_task: asyncio.Task | None = None
        self._rewards = RewardService(self)
        self._broadcaster_token = False
        self._stream_watch_task: asyncio.Task | None = None
        self._memory_task: asyncio.Task | None = None
        self._chat_watch_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._chat_revive_lock = asyncio.Lock()
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

    # --- cooldowns ---------------------------------------------------------

    # Scopes are independent: waiting out a Gemini command does not block !help-bot.
    # The scope is the command's class (KIND_LOCAL / KIND_GEMINI), passed explicitly.
    def cooldown_remaining(self, user: str, scope: str) -> float:
        expiry = self._cooldowns.get(f'{scope}:{user}')
        if expiry is None:
            return 0.0
        return max(0.0, expiry - time.time())

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None:
        now = time.time()
        if len(self._cooldowns) > 500:
            self._cooldowns = {k: e for k, e in self._cooldowns.items() if e > now}
        self._cooldowns[f'{scope}:{user}'] = now + seconds

    def clear_cooldown(self, user: str, scope: str) -> None:
        self._cooldowns.pop(f'{scope}:{user}', None)

    async def process_commands(self, payload) -> None:
        """twitchio's built-in command parsing is switched off.

        Commands are parsed by our own registry in src/core/component.py; twitchio
        has none. Without this, on every «!…» in chat and every reward redemption
        twitchio would look up the command itself and log CommandNotFound with a
        traceback. Component listeners receive events separately and are unaffected.
        """

    # --- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        await init_db()
        if Twitch.BOT_TOKEN and Twitch.BOT_REFRESH:
            await self.add_token(Twitch.BOT_TOKEN, Twitch.BOT_REFRESH)
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
        """The channel's token for rewards. Checks it belongs to the channel and has the scope.

        First takes what twitchio has already loaded from .tio.tokens.json: after
        an OAuth login via the link the token lands there on a graceful shutdown,
        and it is fresher than the .env values, which twitchio does not touch
        after a refresh. If it is empty there, falls back to .env.

        Without the check a foreign or under-scoped token would only surface on
        the first redemption – as an obscure Twitch error in the middle of a stream.
        """
        if not Rewards.ENABLED:
            return
        stored = self._http._tokens.get(self._channel_id)
        if stored:
            token, refresh = stored['token'], stored['refresh']
        elif Twitch.BROADCASTER_TOKEN and Twitch.BROADCASTER_REFRESH:
            token, refresh = Twitch.BROADCASTER_TOKEN, Twitch.BROADCASTER_REFRESH
        else:
            return
        try:
            payload = await self.add_token(token, refresh)
        except Exception as e:
            logger.warning('Токен канала не принят, награды за баллы выключены: %s', e)
            return
        if str(payload.user_id) != self._channel_id:
            logger.warning(
                'TWITCH_BROADCASTER_TOKEN выдан аккаунту %s, а не каналу %s – награды за баллы выключены',
                payload.login, Twitch.CHANNEL,
            )
            return
        if REWARDS_SCOPE not in payload.scopes:
            logger.warning('В токене канала нет права %s – награды за баллы выключены', REWARDS_SCOPE)
            return
        self._broadcaster_token = True

    # --- stream -----------------------------------------------------------

    async def fetch_live_stream(self) -> tuple[str, float] | None:
        """The channel's live stream according to Twitch: (id, start) or None. Does not swallow errors."""
        streams = await self.fetch_streams(user_ids=[self._channel_id], type='live')
        if not streams:
            return None
        return streams[0].id, streams[0].started_at.timestamp()

    async def _sync_stream(self) -> None:
        """Whether the stream is live right now.

        Events about what happened while the bot was down will never arrive:
        the stream may have started or ended without it. On a request error we
        assume there is no stream, and the watch_stream() check fixes it in a few minutes.
        """
        try:
            live = await self.fetch_live_stream()
        except Exception:
            logger.exception('Не удалось узнать, идёт ли эфир – считаю, что нет, сверка поправит')
            return
        if live:
            await self.stream.online(*live)
        else:
            await self.stream.settle_missed_end()

    async def stream_went_online(self, stream_id: str, started_at: float) -> None:
        """Stream is live: stream session, rewards open, perks from the previous one."""
        await self.stream.online(stream_id, started_at)
        await self._rewards.set_open(True)
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
        if Memory.ENABLED and not _running(self._memory_task):
            # event_ready fires again on a reconnect – one check is enough
            self._memory_task = asyncio.create_task(memory.memory_loop())

    async def _stream_end_from_vod(self, stream_id: str, started_at: float) -> float | None:
        """The stream's end from its Twitch recording: recording start plus duration.

        twitchio does not expose a video's stream id, so the recording is matched by
        start time. No recording (VODs off) – None, the tracker estimates the end from chat.
        """
        videos = await self.fetch_videos(user_id=self._channel_id, type='archive', first=5)
        for video in videos:
            if abs(video.created_at.timestamp() - started_at) <= VOD_MATCH_SECONDS:
                return video.created_at.timestamp() + _duration_seconds(video.duration)
        return None

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

        twitchio starts its OAuth adapter as web.AppRunner(..., handle_signals=True),
        and aiohttp installs its own handlers, replacing whatever was there – so the
        ones run_bot() set before start() were dead from that moment on. The bot still
        stopped, but through asyncio.run()'s emergency cleanup instead of our own
        orderly shutdown, and nothing said a signal had arrived (2026-09-20).
        """
        if self.on_shutdown_signal is None:
            return
        loop = asyncio.get_running_loop()
        for sig, name in ((signal.SIGTERM, 'SIGTERM'), (signal.SIGINT, 'SIGINT')):
            loop.add_signal_handler(sig, self.on_shutdown_signal, name)

    async def event_ready(self) -> None:
        self.install_signal_handlers()
        users = await self.fetch_users(ids=[self.bot_id])
        if users:
            self._bot_name = users[0].name
        logger.info('Бот запущен | Ник: %s | Сессия: %s', self._bot_name or self.bot_id, self.session_id)
        try:
            await self._subscribe_to_chat()
        except Exception as e:
            logger.warning('Не удалось подписаться на чат: %s', e)
            logger.warning(
                'Токена нет. Открой в браузере и войди под аккаунтом бота:\n'
                'http://localhost:4343/oauth?scopes=%s&force_verify=true', OAUTH_SCOPES,
            )
        self._start_background_tasks()
        # Memory of chat conversations, including those that ended while the bot was down
        self._start_memory()
        await self._start_rewards()
        if self.stream_live:
            # Perks from the previous stream. What was granted is not granted twice,
            # so a restart or reconnect mid-stream will not duplicate them
            await perks.on_stream_start(self, self.session_id)

    def _start_background_tasks(self) -> None:
        if not self._channel_id:
            if Proactive.ENABLED or Emote.SPAM_ENABLED or Rewards.ENABLED:
                logger.warning('ID канала не получен – фоновые задачи не запущены')
            return
        if HEARTBEAT_PATH and not _running(self._heartbeat_task):
            self._heartbeat_task = asyncio.create_task(self._beat())
        if not _running(self._chat_watch_task):
            self._chat_watch_task = asyncio.create_task(self._watch_chat_socket())
        # The Twitch check catches a missed stream start or end
        if not _running(self._stream_watch_task):
            self._stream_watch_task = asyncio.create_task(watch_stream(self))
        if Proactive.ENABLED and not _running(self._proactive_task):
            self._proactive_task = asyncio.create_task(proactive_loop(self))
            logger.info('Проактивные сообщения включены (раз в %d–%d мин)',
                        Proactive.INTERVAL_MIN_MINUTES, Proactive.INTERVAL_MAX_MINUTES)
        # Curses come from rewards and from the previous stream's results
        if (Rewards.ENABLED or Roll.PERKS_ENABLED) and not _running(self._curse_lift_task):
            self._curse_lift_task = asyncio.create_task(curse_lift_loop(self))
            logger.info('Оповещения о снятии проклятий включены')
        if Help.ANNOUNCE_ENABLED and not _running(self._help_task):
            self._help_task = asyncio.create_task(help_loop(self))
            logger.info('Напоминания о командах включены (раз в %d мин)', Help.ANNOUNCE_INTERVAL_MINUTES)
        if Emote.SPAM_ENABLED and not _running(self._emote_spam_task):
            if Content.items('emotes'):
                self._emote_spam_task = asyncio.create_task(emote_spam_loop(self))
                logger.info('Спам эмотами включён (раз в %d–%d мин)',
                            Emote.SPAM_INTERVAL_MIN_MINUTES, Emote.SPAM_INTERVAL_MAX_MINUTES)
            else:
                logger.warning(
                    'EMOTE_SPAM_ENABLED=true, но список эмотов в CONTENT.md пуст – '
                    'задача не запущена'
                )

    async def _start_rewards(self) -> None:
        # event_ready also fires after a reconnect – start() survives that
        if not Rewards.ENABLED or self._rewards.active or not self._channel_id:
            return
        if not self._broadcaster_token:
            logger.warning(
                'Награды за баллы канала выключены: нет токена канала. Открой в браузере '
                'и войди под аккаунтом канала %s:\n'
                'http://localhost:4343/oauth?scopes=%s&force_verify=true',
                Twitch.CHANNEL, REWARDS_SCOPE,
            )
            return
        try:
            await self._rewards.start(self._channel_id, open_=self.stream_live)
        except Exception:
            logger.exception('Награды за баллы канала не запущены')

    async def event_oauth_authorized(self, payload: twitchio.authentication.UserTokenPayload):
        await self.add_token(payload.access_token, payload.refresh_token)
        if str(payload.user_id) == str(self.bot_id):
            await self._store_tokens('бота', 'TWITCH_BOT_TOKEN', payload)
            if self._channel_id:
                await self._subscribe_to_chat()
        elif self._channel_id and str(payload.user_id) == self._channel_id:
            await self._store_tokens('канала', 'TWITCH_BROADCASTER_TOKEN', payload)
            self._broadcaster_token = True
            await self._start_rewards()

    async def _store_tokens(self, whose: str, env_name: str,
                            payload: twitchio.authentication.UserTokenPayload) -> None:
        """Save the tokens to disk and say so, without putting them in the output.

        They used to be printed in full for copying into .env. In a terminal that
        only reached the scrollback; in a container the same lines land in
        `docker logs`, which keeps them on disk across restarts. So they are written
        where twitchio reads them from anyway, the file is made owner-only, and the
        log gets the last four characters – enough to tell one token from another
        (2026-09-20).
        """
        await self.save_tokens()
        path = Path(TOKENS_FILE)
        try:
            path.chmod(0o600)
        except OSError as e:
            logger.warning('Не удалось ограничить права на %s: %s', path, e)
        logger.info(
            'Токен %s получен (…%s) и сохранён в %s. Значение для %s в .env возьми оттуда',
            whose, payload.access_token[-4:], path, env_name,
        )

    async def close(self, **options) -> None:
        # The chat watch must not resubscribe a bot that is shutting down
        self._shutting_down = True
        # Pause rewards before the HTTP session closes: without the bot a redemption
        # would take the points, and nobody would be there to apply it
        await self._rewards.stop()
        await super().close(**options)

    async def send_chat_message(self, text: str) -> bool:
        """Send without a reply (HTTP API). True if it went through.

        Everything the bot says on its own goes through here – proactive remarks, the
        second and later chunks of an answer, emotes, announcements – with no nick in
        front of it. So this is where a line that would read as a chat command is
        defused: the one place that covers all of them (2026-09-20).
        """
        if not self._channel_id:
            logger.warning('Отправка невозможна: ID канала не получен')
            return False
        text = defuse(text)
        if not text:
            return False
        try:
            await self._http.post_chat_message(
                broadcaster_id=self._channel_id,
                sender_id=str(self.bot_id),
                message=text,
                token_for=str(self.bot_id),
            )
            return True
        except Exception:
            logger.exception('Не удалось отправить сообщение в чат')
            return False

    # --- the chat connection ------------------------------------------------
    # twitchio 3.2.1 gives up on an EventSub websocket for good in several cases and
    # says so only with a websocket_closed event: a reconnect whose welcome from
    # Twitch takes over 11 seconds (the owner's uplink is saturated during streams),
    # a revoked subscription, a close code it does not retry. The bot then goes on
    # running – proactive remarks and emotes go out over HTTP – but hears nothing:
    # on 2026-09-19 chat from 23:55 to the restart at 23:58 never reached it. A dead
    # socket keeps _closed set (and may linger in the client's registry under a stale
    # session id), while one that is still retrying does not, so only when no socket
    # of the bot is left open are the subscriptions made again on a fresh one.

    def _chat_socket_alive(self) -> bool:
        sockets = self._websockets.get(str(self.bot_id), {})
        return any(not socket._closed for socket in sockets.values())

    async def _revive_chat(self) -> None:
        if self._shutting_down or not self._channel_id or self._chat_socket_alive():
            return
        async with self._chat_revive_lock:
            if self._shutting_down or self._chat_socket_alive():
                return
            logger.warning('Соединение с чатом Twitch потеряно – подписываюсь заново')
            # Dead sockets out of the registry, or subscribe_websocket() would pick one
            self._websockets.pop(str(self.bot_id), None)
            await self._subscribe_to_chat()
            logger.warning('Чат снова подключён')

    def _background_tasks(self) -> list[asyncio.Task]:
        return [t for t in (
            self._proactive_task, self._emote_spam_task, self._help_task,
            self._curse_lift_task, self._stream_watch_task, self._memory_task,
            self._chat_watch_task, self._heartbeat_task,
        ) if t is not None]

    async def stop_background_tasks(self) -> None:
        """Cancel the loops and wait for them, before the database is closed.

        Nothing used to cancel them. A loop waking up after close_db() calls get_db(),
        which opens a fresh connection and with it a new non-daemon aiosqlite thread –
        and the process never exits. That is what once looked like a Gemini call hanging
        for minutes (2026-09-20).
        """
        tasks = self._background_tasks()
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning('Фоновая задача завершилась с ошибкой: %r', result)
        logger.info('Фоновые задачи остановлены: %d', len(tasks))

    async def _beat(self) -> None:
        """Touch a file so the container healthcheck can see the bot is still running.

        Started only when BOT_HEARTBEAT is set, so nothing changes outside a container.
        It beats from _start_background_tasks(), that is once the channel id is known and
        the loops are up: a process that came up without them must read as unhealthy,
        because «alive but deaf» is exactly what restart policies do not catch (2026-09-20).
        """
        path = Path(HEARTBEAT_PATH)
        logger.info('Heartbeat включён: %s (раз в %d с)', path, HEARTBEAT_SECONDS)
        while True:
            try:
                path.touch()
            except OSError as e:
                # A full or read-only volume: say so, but do not take the bot down
                logger.warning('Не удалось обновить heartbeat: %s', e)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _watch_chat_socket(self) -> None:
        logger.info('Проверка соединения с чатом включена (раз в %d с)', CHAT_WATCH_SECONDS)
        while True:
            await asyncio.sleep(CHAT_WATCH_SECONDS)
            try:
                await self._revive_chat()
            except Exception as e:
                # Twitch or the network is still down: the next check tries again
                logger.warning('Переподписка на чат не удалась: %s', e)

    async def event_websocket_closed(self, payload) -> None:
        # Fired for every close, a normal reconnect included; a socket that is
        # reconnecting stays open for _chat_socket_alive(), so this only speeds
        # up the watch when the socket was given up
        if self._shutting_down:
            return
        await asyncio.sleep(5)
        try:
            await self._revive_chat()
        except Exception as e:
            logger.warning('Переподписка на чат не удалась: %s', e)

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
            logger.warning(
                'Не удалось подписаться на фоловы: %s\n'
                'Переавторизация: http://localhost:4343/oauth?scopes=%s&force_verify=true',
                e, OAUTH_SCOPES_FOLLOWS,
            )

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


def _running(task: asyncio.Task | None) -> bool:
    return task is not None and not task.done()


def _duration_seconds(duration: str) -> int:
    """A Twitch video duration like «3h8m33s» in seconds."""
    units = {'h': 3600, 'm': 60, 's': 1}
    return sum(int(value) * units[unit] for value, unit in re.findall(r'(\d+)([hms])', duration))


async def run_bot() -> None:
    setup_logging('INFO')
    validate_config()
    validate_content()
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(name: str) -> None:
        # Logged in the handler itself: without it there is no way to tell a signal
        # that arrived from a process that stopped for its own reasons (2026-09-20)
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
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
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
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            pass
