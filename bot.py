import asyncio
import logging
import re
import signal
import time

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from src.core.component import ChatComponent
from src.core.config import Emote, Help, Proactive, Rewards, Roll, Twitch, validate_config
from src.core.content import Content, validate_content
from src.core.database import close_db, init_db
from src.core.logging_setup import setup_logging
from src.core.stream import StreamTracker, watch_stream
from src.gemini.proactive import proactive_loop
from src.local.emote_spam import emote_spam_loop
from src.local.help_announce import help_loop
from src.local.roll import perks
from src.local.roll.announce import curse_lift_loop
from src.local.roll.rewards import REWARDS_SCOPE, RewardComponent, RewardService

logger = logging.getLogger(__name__)

OAUTH_SCOPES = 'user:read:chat+user:write:chat+user:bot'
OAUTH_SCOPES_FOLLOWS = f'{OAUTH_SCOPES}+moderator:read:followers'

# Запись эфира узнаётся по времени начала: у VOD оно совпадает со стартом стрима
VOD_MATCH_SECONDS = 300


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
        self.stream = StreamTracker(end_lookup=self._stream_end_from_vod)

    @property
    def session_id(self) -> str:
        """Эфир, пока идёт стрим, иначе текущая дата. См. src/core/stream.py."""
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

    # --- кулдауны ---------------------------------------------------------

    # Области независимы: отсидка за Gemini-команду не мешает жать !help-bot.
    # Область – это класс команды (KIND_LOCAL / KIND_GEMINI), передаётся явно.
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
        """Встроенный разбор команд twitchio выключен.

        Команды разбирает свой реестр в src/core/component.py, у twitchio их
        нет. Без этого twitchio на каждое «!…» в чате и на каждый выкуп награды
        искал бы команду у себя и писал в лог CommandNotFound с трейсбеком.
        Слушатели компонентов получают события отдельно и этим не задеты.
        """

    # --- жизненный цикл ---------------------------------------------------

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
        """Токен канала для наград. Проверяем, что он того канала и с нужным правом.

        Сначала берём то, что twitchio уже загрузил из .tio.tokens.json: после
        авторизации по ссылке токен попадает туда при штатной остановке, и он
        свежее значений в .env, которые twitchio после обновления не трогает.
        Там пусто – берём из .env.

        Без проверки чужой или урезанный токен всплыл бы только при первом
        выкупе – невнятной ошибкой Twitch посреди эфира.
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

    # --- эфир -------------------------------------------------------------

    async def fetch_live_stream(self) -> tuple[str, float] | None:
        """Идущий эфир канала по данным Twitch: (id, начало) или None. Ошибки не глотает."""
        streams = await self.fetch_streams(user_ids=[self._channel_id], type='live')
        if not streams:
            return None
        return streams[0].id, streams[0].started_at.timestamp()

    async def _sync_stream(self) -> None:
        """Идёт ли эфир прямо сейчас.

        События о том, что случилось, пока бот был выключен, уже не придут:
        стрим мог начаться или закончиться без него. Ошибка запроса – считаем,
        что эфира нет, а сверка watch_stream() поправит через несколько минут.
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
        """Эфир идёт: сессия эфира, награды открыты, бонусы по итогам прошлого."""
        await self.stream.online(stream_id, started_at)
        await self._rewards.set_open(True)
        await perks.on_stream_start(self, self.session_id)

    async def stream_went_offline(self) -> None:
        """Эфир закончился: сессия по дате, игра закрыта, награды на паузе."""
        await self.stream.offline()
        await self._rewards.set_open(False)

    async def _stream_end_from_vod(self, stream_id: str, started_at: float) -> float | None:
        """Конец эфира по его записи в Twitch: начало записи плюс длительность.

        twitchio не отдаёт у видео id эфира, поэтому запись узнаётся по времени
        начала. Записи нет (VOD выключены) – None, трекер оценит конец по чату.
        """
        videos = await self.fetch_videos(user_id=self._channel_id, type='archive', first=5)
        for video in videos:
            if abs(video.created_at.timestamp() - started_at) <= VOD_MATCH_SECONDS:
                return video.created_at.timestamp() + _duration_seconds(video.duration)
        return None

    async def event_stream_online(self, payload) -> None:
        # Повтор трансляции и премьера – не эфир со стримером
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

    async def event_ready(self) -> None:
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
        await self._start_rewards()
        if self.stream_live:
            # Бонусы по итогам прошлого эфира. Выданное второй раз не выдаётся,
            # поэтому перезапуск и переподключение посреди стрима их не задвоят
            await perks.on_stream_start(self, self.session_id)

    def _start_background_tasks(self) -> None:
        if not self._channel_id:
            if Proactive.ENABLED or Emote.SPAM_ENABLED or Rewards.ENABLED:
                logger.warning('ID канала не получен – фоновые задачи не запущены')
            return
        # Сверка с Twitch ловит пропущенные начало и конец эфира
        if not _running(self._stream_watch_task):
            self._stream_watch_task = asyncio.create_task(watch_stream(self))
        if Proactive.ENABLED and not _running(self._proactive_task):
            self._proactive_task = asyncio.create_task(proactive_loop(self))
            logger.info('Проактивные сообщения включены (раз в %d–%d мин)',
                        Proactive.INTERVAL_MIN_MINUTES, Proactive.INTERVAL_MAX_MINUTES)
        # Проклятия бывают от наград и от итогов прошлого эфира
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
        # event_ready приходит и после переподключения – start() это переживает
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
            print(
                f'\nAdd to .env:\n'
                f'TWITCH_BOT_TOKEN={payload.access_token}\n'
                f'TWITCH_BOT_REFRESH={payload.refresh_token}\n'
            )
            if self._channel_id:
                await self._subscribe_to_chat()
        elif self._channel_id and str(payload.user_id) == self._channel_id:
            print(
                f'\nAdd to .env:\n'
                f'TWITCH_BROADCASTER_TOKEN={payload.access_token}\n'
                f'TWITCH_BROADCASTER_REFRESH={payload.refresh_token}\n'
            )
            self._broadcaster_token = True
            await self._start_rewards()

    async def close(self, **options) -> None:
        # Награды на паузу до закрытия HTTP-сессии: без бота выкуп списал бы
        # баллы, а применить его было бы некому
        await self._rewards.stop()
        await super().close(**options)

    async def send_chat_message(self, text: str) -> bool:
        """Отправка без реплая (HTTP API). True – если ушло."""
        if not self._channel_id:
            logger.warning('Отправка невозможна: ID канала не получен')
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
    """Длительность видео Twitch вида «3h8m33s» в секундах."""
    units = {'h': 3600, 'm': 60, 's': 1}
    return sum(int(value) * units[unit] for value, unit in re.findall(r'(\d+)([hms])', duration))


async def run_bot() -> None:
    setup_logging('INFO')
    validate_config()
    validate_content()
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    try:
        async with Bot() as bot:
            bot_task = asyncio.create_task(bot.start())
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            done, _ = await asyncio.wait(
                [bot_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                logger.info('Получен сигнал остановки, завершаюсь...')
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
    finally:
        await close_db()


if __name__ == '__main__':
    from src.cli.main import main as cli_main
    if not cli_main():
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            pass
