"""The Discord side of the bot: the voice channel and the owner's commands."""
import asyncio
import contextlib
import logging
from enum import StrEnum

import aiohttp
import discord

from src.core import speech
from src.core.config import Discord, Voice
from src.core.content import Content
from src.core.database import get_state, set_state
from src.discord.local.voice.speaker import Speaker
from src.discord.local.voice.tts import TTSClient

logger = logging.getLogger(__name__)

# A voice connection that has not come up by then is given up; the next check retries
CONNECT_TIMEOUT = 20
# How often the voice channel is checked against the owner's choice: a connection that
# dropped for good fires no event, and the bot would otherwise stay out until a restart
PRESENCE_CHECK_SECONDS = 60
# bot_state key of the owner's choice: 'on' after !join, 'off' after !leave
VOICE_STATE_KEY = 'discord_voice'
# The owner's command is answered with a reaction: nothing to read, nothing to translate
OK, NO, MUTED = '✅', '❌', '🔇'
# !help answers anyone in the text channel, at most once per this many seconds
HELP_COOLDOWN_SECONDS = 10
# A voice command without the rights is refused once per person per this many seconds
REFUSAL_COOLDOWN_SECONDS = 30
# A start that failed is retried after this pause, doubling up to the cap
RETRY_SECONDS = 30
RETRY_MAX_SECONDS = 600


class Move(StrEnum):
    JOIN = 'join'
    LEAVE = 'leave'
    STAY = 'stay'


def presence(wanted: bool, connected: bool) -> Move:
    """Where the bot should be: in the voice channel exactly while the owner wants it there."""
    if wanted and not connected:
        return Move.JOIN
    if connected and not wanted:
        return Move.LEAVE
    return Move.STAY


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
        # The owner's choice; None until read from the database, which the Twitch side
        # opens at its own start – until then the bot stays where it is
        self._wanted: bool | None = None
        # Leaving the channel on close fires a voice event: it must not bring the bot back
        self._closing = False
        self._help_at = float('-inf')
        self._refused_at: dict[int, float] = {}

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

    def _voice_client(self) -> discord.VoiceClient | None:
        guild = self.get_guild(Discord.GUILD_ID)
        client = guild.voice_client if guild else None
        return client if isinstance(client, discord.VoiceClient) else None

    def _voice_channel(self) -> discord.VoiceChannel | None:
        channel = self.get_channel(Discord.VOICE_CHANNEL_ID)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    async def on_ready(self) -> None:
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
            await asyncio.sleep(PRESENCE_CHECK_SECONDS)
            try:
                await self._reconcile()
            except Exception:
                logger.exception('Discord: проверка голосового канала упала')

    async def _load_wanted(self) -> None:
        if self._wanted is not None:
            return
        try:
            self._wanted = await get_state(VOICE_STATE_KEY) == 'on'
        except Exception as e:
            # The database is not open yet: the Twitch side opens it a moment after start
            logger.debug('Discord: выбор голосового канала пока не прочитан: %s', e)

    async def _save_wanted(self, wanted: bool) -> None:
        self._wanted = wanted
        try:
            await set_state(VOICE_STATE_KEY, 'on' if wanted else 'off')
        except Exception:
            logger.exception('Discord: выбор голосового канала не сохранён – действует до перезапуска')

    async def _reconcile(self) -> None:
        """Put the bot where the owner's choice says: in the channel or out of it."""
        if self.speaker is None or self._closing:
            return
        async with self._presence_lock:
            await self._load_wanted()
            if self._wanted is None:
                return
            client = self._voice_client()
            move = presence(self._wanted, client is not None and client.is_connected())
            if move is Move.JOIN:
                channel = self._voice_channel()
                if channel is not None:
                    await self._connect(channel)
            elif move is Move.LEAVE:
                await self._disconnect()

    async def _connect(self, channel: discord.VoiceChannel) -> bool:
        client = self._voice_client()
        if client is not None and client.is_connected():
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
            await self._help(message)
            return
        if not words or words[0] not in ('!join', '!leave', '!tts'):
            return
        if not may_command(message.author):
            await self._refuse(message)
            return
        reaction = await self._command(words[0], words[1:])
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction(reaction)

    async def _help(self, message: discord.Message) -> None:
        """The list of commands, for anyone in the text channel; one answer per cooldown."""
        now = asyncio.get_running_loop().time()
        if now - self._help_at < HELP_COOLDOWN_SECONDS:
            return
        self._help_at = now
        text = Content.text('discord_help')
        if not text:
            return
        with contextlib.suppress(discord.HTTPException):
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _refuse(self, message: discord.Message) -> None:
        """Tell someone without the rights that the voice commands are not theirs, once per cooldown."""
        now = asyncio.get_running_loop().time()
        if now - self._refused_at.get(message.author.id, float('-inf')) < REFUSAL_COOLDOWN_SECONDS:
            return
        self._refused_at[message.author.id] = now
        text = Content.text('discord_no_rights')
        if not text:
            return
        with contextlib.suppress(discord.HTTPException):
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _command(self, name: str, args: list[str]) -> str:
        """The owner's voice commands; returns the reaction that answers it."""
        if self.speaker is None:
            return NO
        if name == '!join':
            async with self._presence_lock:
                await self._save_wanted(True)
                channel = self._voice_channel()
                # A failed connect is retried by the presence check: the choice is kept
                return OK if channel is not None and await self._connect(channel) else NO
        if name == '!leave':
            async with self._presence_lock:
                await self._save_wanted(False)
                await self._disconnect()
            return OK
        # !tts on|off; bare !tts shows the state
        if args[:1] == ['on']:
            self.speaker.enabled = True
        elif args[:1] == ['off']:
            self.speaker.enabled = False
            self.speaker.clear()
            client = self._voice_client()
            if client is not None and client.is_playing():
                client.stop()
        elif args:
            return NO
        return OK if self.speaker.enabled else MUTED


class DiscordService:
    """Keeps a Discord connection up for as long as the bot runs.

    discord.py reconnects a session that dropped, but a start that failed – no network
    yet after a reboot, Discord down – raises and leaves nothing running. The service
    then starts a fresh client after a pause. A rejected token or a missing privileged
    intent is not retried: it needs the owner's hand in the Developer Portal.
    """

    def __init__(self) -> None:
        self.bot: DiscordBot | None = None
        self._stopping = False

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
                logger.warning('Discord не поднялся (%s: %s) – новая попытка через %d с', type(e).__name__, e, delay)
            finally:
                with contextlib.suppress(Exception):
                    await self.bot.close()
            if self._stopping:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def stop(self) -> None:
        self._stopping = True
        if self.bot is not None:
            await self.bot.close()
