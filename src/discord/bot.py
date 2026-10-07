"""The Discord side of the bot: the voice channel and the owner's commands."""
import asyncio
import contextlib
import logging
from enum import StrEnum

import aiohttp
import discord

from src.core import speech
from src.core.config import Discord, Voice
from src.discord.local.voice.speaker import Speaker
from src.discord.local.voice.tts import TTSClient

logger = logging.getLogger(__name__)

# A voice connection that has not come up by then is given up; the next voice event retries
CONNECT_TIMEOUT = 20
# The owner's command is answered with a reaction: nothing to read, nothing to translate
OK, NO, MUTED = '✅', '❌', '🔇'


class Move(StrEnum):
    JOIN = 'join'
    LEAVE = 'leave'
    STAY = 'stay'


def presence(connected: bool, owner_here: bool, people_here: bool, held_off: bool) -> Move:
    """Where the bot should be: with the owner in the voice channel, and never alone in it.

    held_off – the owner sent !leave and is still in the channel: the bot does not come
    back until the owner leaves and joins again.
    """
    if connected:
        return Move.STAY if people_here else Move.LEAVE
    return Move.JOIN if owner_here and not held_off else Move.STAY


class DiscordBot(discord.Client):

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._session: aiohttp.ClientSession | None = None
        self.speaker: Speaker | None = None
        self._speaker_task: asyncio.Task | None = None
        self._unlisten = None
        self._presence_lock = asyncio.Lock()
        self._held_off = False

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
        self._speaker_task = asyncio.create_task(self.speaker.run(), name='voice-speaker')
        self._unlisten = speech.listen(self.speaker.submit)

    async def close(self) -> None:
        if self._unlisten:
            self._unlisten()
        if self._speaker_task:
            self._speaker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._speaker_task
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
        await self._follow_owner()

    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        if member.guild.id == Discord.GUILD_ID:
            await self._follow_owner()

    async def _follow_owner(self) -> None:
        if self.speaker is None:
            return
        async with self._presence_lock:
            channel = self._voice_channel()
            if channel is None:
                return
            people = [m for m in channel.members if not m.bot]
            owner_here = any(m.id == Discord.OWNER_ID for m in people)
            if not owner_here:
                self._held_off = False
            client = self._voice_client()
            move = presence(client is not None and client.is_connected(), owner_here, bool(people), self._held_off)
            if move is Move.JOIN:
                await self._connect(channel)
            elif move is Move.LEAVE:
                await self._disconnect()

    async def _connect(self, channel: discord.VoiceChannel) -> bool:
        client = self._voice_client()
        if client is not None and client.is_connected():
            return True
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
            await client.disconnect()
            logger.info('Discord: вышел из голосового канала')

    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != Discord.OWNER_ID or message.channel.id != Discord.TEXT_CHANNEL_ID:
            return
        words = message.content.strip().lower().split()
        if not words or words[0] not in ('!join', '!leave', '!tts'):
            return
        reaction = await self._command(words[0], words[1:])
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction(reaction)

    async def _command(self, name: str, args: list[str]) -> str:
        """The owner's voice commands; returns the reaction that answers it."""
        if self.speaker is None:
            return NO
        if name == '!join':
            channel = self._voice_channel()
            async with self._presence_lock:
                self._held_off = False
                return OK if channel is not None and await self._connect(channel) else NO
        if name == '!leave':
            async with self._presence_lock:
                self._held_off = True
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


async def run_discord(bot: DiscordBot) -> None:
    """Run the Discord bot until it is closed; a failure is logged and leaves Twitch running."""
    try:
        await bot.start(Discord.TOKEN)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Discord остановился из-за ошибки – Twitch работает дальше')
