"""The Discord side of the bot: the voice channel and the owner's commands."""
import asyncio
import contextlib
import logging
import time
from enum import StrEnum

import aiohttp
import discord

from src.core import speech
from src.core.config import Discord, Voice
from src.core.content import Content
from src.core.cooldowns import Cooldowns
from src.core.database import get_state, set_state
from src.discord.local.voice.speaker import Speaker
from src.discord.local.voice.tts import TTSClient

logger = logging.getLogger(__name__)

# A voice connection that has not come up by then is given up; the next check retries
CONNECT_TIMEOUT = 20
# How often the voice channel is checked against the owner's choice: a connection that
# dropped for good fires no event, and the bot would otherwise stay out until a restart.
# While the connection is unsettled (lost, or in another channel) it is checked more often,
# so the grace periods below are counted from close to the real moment
PRESENCE_CHECK_SECONDS = 60
UNSETTLED_CHECK_SECONDS = 10
# A voice client that lost its connection is left to discord.py's own reconnect this long,
# and one dragged to another channel is left to finish discord.py's own move handling,
# before the bot acts: two connect flows at once fight over the channel
RECONNECT_GRACE_SECONDS = 90
MOVE_GRACE_SECONDS = 10
# bot_state keys of the owner's choices, 'on' / 'off': the voice channel (!join / !leave)
# and the voice itself (!voice; until the first one – VOICE_ENABLED)
VOICE_STATE_KEY = 'discord_voice'
VOICE_ENABLED_KEY = 'discord_voice_enabled'
# The owner's command is answered with a reaction: nothing to read, nothing to translate
OK, NO, MUTED = '✅', '❌', '🔇'
# !help answers anyone in the text channel, at most once per this many seconds
HELP_COOLDOWN_SECONDS = 10
# A voice command without the rights is refused once per person per this many seconds
REFUSAL_COOLDOWN_SECONDS = 30
# A start that failed is retried after this pause, doubling up to the cap; a session that
# came up resets it
RETRY_SECONDS = 30
RETRY_MAX_SECONDS = 600


class Move(StrEnum):
    JOIN = 'join'
    LEAVE = 'leave'
    STAY = 'stay'


class Where(StrEnum):
    """Where the bot's voice connection is."""
    OUT = 'out'                # no voice client at all
    HERE = 'here'              # connected to DISCORD_VOICE_CHANNEL_ID
    ELSEWHERE = 'elsewhere'    # connected in another channel for longer than MOVE_GRACE_SECONDS
    RECOVERING = 'recovering'  # lost or moved just now: discord.py is still handling it
    DOWN = 'down'              # lost its connection for longer than RECONNECT_GRACE_SECONDS


def presence(wanted: bool, where: Where) -> Move:
    """In the voice channel exactly while the owner wants it there, and in that channel."""
    if not wanted:
        return Move.STAY if where is Where.OUT else Move.LEAVE
    return Move.STAY if where in (Where.HERE, Where.RECOVERING) else Move.JOIN


def may_command(author) -> bool:
    """The owner, or a member holding one of DISCORD_COMMAND_ROLE_IDS.

    A guild message carries its author's roles, so no members intent is needed.
    """
    if author.id == Discord.OWNER_ID:
        return True
    return any(role.id in Discord.COMMAND_ROLE_IDS for role in getattr(author, 'roles', ()))


class DiscordBot(discord.Client):
    """One connection to Discord; DiscordService makes a new one after a failure."""

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._session: aiohttp.ClientSession | None = None
        self.speaker: Speaker | None = None
        self._tasks: list[asyncio.Task] = []
        self._unlisten = None
        self._presence_lock = asyncio.Lock()
        # The owner's choice of the voice channel, read in setup_hook
        self._wanted = False
        # Since when the voice client has been lost or in another channel (monotonic)
        self._unsettled_since: float | None = None
        # Leaving the channel on close fires a voice event: it must not bring the bot back
        self._closing = False
        self._cooldowns = Cooldowns()
        # Set once the session came up: DiscordService then starts the next retry from scratch
        self.was_ready = False

    async def setup_hook(self) -> None:
        if not Voice.TTS_URL:
            logger.info('Discord: VOICE_TTS_URL не задан – голос выключен')
            return
        if not discord.opus.is_loaded():
            try:
                discord.opus.load_opus(Discord.OPUS_LIB)
            except OSError as e:
                logger.error('Discord: libopus не загружена (%s: %s) – голос выключен', Discord.OPUS_LIB, e)
                return
        self._session = aiohttp.ClientSession()
        self.speaker = Speaker(self._voice_client, TTSClient(self._session, Voice.TTS_URL, Voice.TTS_VOICE))
        # run_bot() has brought the database up before any platform started
        await self.load_state()
        self._tasks = [
            asyncio.create_task(self.speaker.run(), name='voice-speaker'),
            asyncio.create_task(self._presence_loop(), name='voice-presence'),
        ]
        self._unlisten = speech.listen(self.speaker.submit)

    async def close(self) -> None:
        self._closing = True
        if self._unlisten:
            self._unlisten()
            self._unlisten = None
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        client = self._voice_client()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect(force=True)
        await super().close()
        if self._session:
            await self._session.close()

    def _voice_channel(self) -> discord.VoiceChannel | None:
        channel = self.get_channel(Discord.VOICE_CHANNEL_ID)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    def _voice_client(self) -> discord.VoiceClient | None:
        # The bot keeps one voice connection; it is found among its own, so it stays
        # visible – to !leave and to close() – even when the configured channel is not
        for client in self.voice_clients:
            if isinstance(client, discord.VoiceClient):
                return client
        return None

    def _where(self) -> Where:
        client = self._voice_client()
        connected = client is not None and client.is_connected()
        if client is None or (connected and client.channel is not None
                              and client.channel.id == Discord.VOICE_CHANNEL_ID):
            self._unsettled_since = None
            return Where.OUT if client is None else Where.HERE
        now = time.monotonic()
        if self._unsettled_since is None:
            self._unsettled_since = now
        grace = MOVE_GRACE_SECONDS if connected else RECONNECT_GRACE_SECONDS
        if now - self._unsettled_since < grace:
            return Where.RECOVERING
        return Where.ELSEWHERE if connected else Where.DOWN

    async def on_ready(self) -> None:
        self.was_ready = True
        logger.info('Discord: вошёл как %s', self.user)
        if self._voice_channel() is None:
            logger.error('Discord: голосовой канал %s не найден или недоступен боту', Discord.VOICE_CHANNEL_ID)
        await self._reconcile()

    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        # Only the bot's own state matters: a dropped or moved connection is put back at once
        if self.user is not None and member.id == self.user.id:
            await self._reconcile()

    async def _presence_loop(self) -> None:
        while True:
            unsettled = self._unsettled_since is not None
            await asyncio.sleep(UNSETTLED_CHECK_SECONDS if unsettled else PRESENCE_CHECK_SECONDS)
            try:
                await self._reconcile()
            except Exception:
                logger.exception('Discord: проверка голосового канала упала')

    async def load_state(self) -> None:
        """The owner's saved choices – the voice channel and the voice itself."""
        try:
            wanted = await get_state(VOICE_STATE_KEY)
            enabled = await get_state(VOICE_ENABLED_KEY)
        except Exception:
            logger.exception('Discord: сохранённый выбор не прочитан – бот вне канала, голос по VOICE_ENABLED')
            return
        self._wanted = wanted == 'on'
        if enabled is not None and self.speaker is not None:
            self.speaker.enabled = enabled == 'on'

    async def _save(self, key: str, on: bool) -> None:
        try:
            await set_state(key, 'on' if on else 'off')
        except Exception:
            logger.exception('Discord: выбор %s не сохранён – действует до перезапуска', key)

    async def _reconcile(self) -> None:
        """Put the bot where the owner's choice says: in the channel or out of it."""
        if self.speaker is None or self._closing:
            return
        async with self._presence_lock:
            move = presence(self._wanted, self._where())
            if move is Move.JOIN:
                channel = self._voice_channel()
                if channel is not None:
                    await self._connect(channel)
            elif move is Move.LEAVE:
                await self._disconnect()

    async def _connect(self, channel: discord.VoiceChannel) -> bool:
        client = self._voice_client()
        if client is not None and client.is_connected():
            if client.channel is not None and client.channel.id == channel.id:
                return True
            # Dragged to another channel: discord.py keeps the connection, only moves it.
            # A move that times out is logged and reverted by discord.py, not raised
            with contextlib.suppress(Exception):
                await client.move_to(channel, timeout=CONNECT_TIMEOUT)
            if client.channel is None or client.channel.id != channel.id:
                logger.warning('Discord: не удалось вернуться в «%s»', channel.name)
                return False
            self._unsettled_since = None
            logger.info('Discord: вернулся в «%s»', channel.name)
            return True
        if client is not None:
            # A client that lost its connection for good: a new one cannot start beside it
            with contextlib.suppress(Exception):
                await client.disconnect(force=True)
        try:
            await channel.connect(self_deaf=True, timeout=CONNECT_TIMEOUT)
        except Exception as e:
            logger.warning('Discord: не удалось зайти в «%s»: %s', channel.name, e)
            return False
        self._unsettled_since = None
        logger.info('Discord: зашёл в «%s»', channel.name)
        return True

    async def _disconnect(self) -> None:
        client = self._voice_client()
        if self.speaker:
            self.speaker.clear()
        if client is not None:
            await client.disconnect(force=True)
            logger.info('Discord: вышел из голосового канала')

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != Discord.TEXT_CHANNEL_ID or message.author.bot:
            return
        words = message.content.strip().lower().split()
        if words[:1] == ['!help']:
            # One answer per cooldown for the whole channel
            await self._reply_once(message, 'discord_help', '*', HELP_COOLDOWN_SECONDS)
            return
        if not words or words[0] not in ('!join', '!leave', '!voice'):
            return
        if not may_command(message.author):
            await self._reply_once(message, 'discord_no_rights', str(message.author.id), REFUSAL_COOLDOWN_SECONDS)
            return
        reaction = await self._command(words[0])
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction(reaction)

    async def _reply_once(self, message: discord.Message, key: str, who: str, seconds: int) -> None:
        """Answer with texts.<key>, at most once per `seconds` for `who`; never pings anyone."""
        if self._cooldowns.remaining(who, key):
            return
        self._cooldowns.set(who, seconds, key)
        text = Content.text(key)
        if not text:
            return
        with contextlib.suppress(discord.HTTPException):
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _command(self, name: str) -> str:
        """The voice commands; returns the reaction that answers it."""
        if self.speaker is None:
            return NO
        if name == '!join':
            async with self._presence_lock:
                self._wanted = True
                await self._save(VOICE_STATE_KEY, True)
                channel = self._voice_channel()
                # A failed connect is retried by the presence check: the choice is kept
                return OK if channel is not None and await self._connect(channel) else NO
        if name == '!leave':
            async with self._presence_lock:
                self._wanted = False
                await self._save(VOICE_STATE_KEY, False)
                await self._disconnect()
            return OK
        # !voice toggles the voice and remembers it; the reaction shows the state it is in now
        self.speaker.enabled = not self.speaker.enabled
        await self._save(VOICE_ENABLED_KEY, self.speaker.enabled)
        if not self.speaker.enabled:
            self.speaker.clear()
            client = self._voice_client()
            if client is not None and client.is_playing():
                client.stop()
        return OK if self.speaker.enabled else MUTED


class DiscordService:
    """Keeps a Discord connection up for as long as the bot runs.

    discord.py reconnects a session that dropped, but a start that failed – no network
    yet after a reboot, Discord down – raises and leaves nothing running. The service
    then starts a fresh client after a pause. A rejected token or a missing privileged
    intent is not retried: it needs the owner's hand in the Developer Portal.
    """

    def __init__(self, sleep=asyncio.sleep) -> None:
        self.bot: DiscordBot | None = None
        self._stopping = False
        self._sleep = sleep

    async def run(self) -> None:
        delay = RETRY_SECONDS
        while not self._stopping:
            self.bot = DiscordBot()
            try:
                await self.bot.start(Discord.TOKEN)
            except asyncio.CancelledError:
                raise
            except discord.LoginFailure:
                logger.error('Discord: токен отклонён – проверь DISCORD_TOKEN; Discord не запущен, Twitch работает')
                return
            except discord.PrivilegedIntentsRequired:
                logger.error('Discord: не включён Message Content Intent в Developer Portal; '
                             'Discord не запущен, Twitch работает')
                return
            except Exception as e:
                failure = f'не поднялся ({type(e).__name__}: {e})'
            else:
                failure = 'сессия закрылась'
            finally:
                with contextlib.suppress(Exception):
                    await self.bot.close()
            if self._stopping:
                return
            if self.bot.was_ready:
                # A session that ran for a while: its end starts the backoff anew
                delay = RETRY_SECONDS
            logger.warning('Discord %s – новая попытка через %d с', failure, delay)
            await self._sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def stop(self) -> None:
        self._stopping = True
        if self.bot is not None:
            await self.bot.close()
