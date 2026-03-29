import asyncio
import logging
import random
import re
import signal
import time
import twitchio

from google.genai import types
from twitchio import eventsub
from twitchio.ext import commands
from src.config import Twitch, Gemini, Cooldown, Context, Caps, Proactive, validate_config
from src.database import (
    init_db, close_db, save_chat_message, save_bot_interaction, save_fact, delete_fact,
    get_relevant_facts, get_recent_chat, search_context,
    get_random_knowledge, get_session_stats, get_total_stats,
    get_user_messages, get_user_interactions,
)
from src.commands import CommandContext, CommandRegistry
from src.context import ContextBuilder
from src.gemini import generate, make_gen_config, SAFETY_OFF
from src.utils import (
    is_caps, caps_preserve_mentions, strip_markdown, split_into_chunks,
    cleanup_response, TWITCH_MSG_MAX, WHO_VERSUS_MAX, CHUNK_SEND_DELAY,
)

logger = logging.getLogger(__name__)

FACT_TRIGGER = '!fact'
DEFACT_TRIGGER = '!defact'
STATS_TRIGGER = '!stat'
HELP_TRIGGER = '!help'
ASK_TRIGGER = '!ask'
SUMMARY_TRIGGER = '!summary'
WHO_TRIGGER = '!who'
VERSUS_TRIGGER = '!versus'
SOSUR_RE = re.compile(r'сосур\w*', re.IGNORECASE | re.UNICODE)


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

    @property
    def session_id(self) -> str:
        return time.strftime('%Y-%m-%d')

    async def setup_hook(self) -> None:
        await init_db()
        if Twitch.BOT_TOKEN and Twitch.BOT_REFRESH:
            await self.add_token(Twitch.BOT_TOKEN, Twitch.BOT_REFRESH)
        users = await self.fetch_users(logins=[Twitch.CHANNEL])
        if users:
            self._channel_id = str(users[0].id)
        await self.add_component(ChatComponent(self))

    async def event_ready(self) -> None:
        users = await self.fetch_users(ids=[self.bot_id])
        if users:
            self._bot_name = users[0].name
        logger.info('Bot started | Username: %s | Session: %s', self._bot_name or self.bot_id, self.session_id)
        try:
            await self._subscribe_to_chat()
        except Exception as e:
            logger.warning('Failed to subscribe to chat: %s', e)
            logger.info(
                'No token found. Open in browser and log in as the bot account:\n'
                'http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true'
            )
        if Proactive.ENABLED and self._channel_id:
            if self._proactive_task and not self._proactive_task.done():
                return
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            logger.info('Proactive messages enabled (every %d min)', Proactive.INTERVAL_MINUTES)

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

    async def _send_chat_message(self, text: str) -> None:
        await self._http.post_chat_message(
            broadcaster_id=self._channel_id,
            sender_id=str(self.bot_id),
            message=text,
            token_for=str(self.bot_id),
        )

    async def _proactive_loop(self) -> None:
        await asyncio.sleep(Proactive.INTERVAL_MINUTES * 60)
        while True:
            try:
                recent_chat = await get_recent_chat(self.session_id, Context.CHAT_MESSAGES)
                if not recent_chat:
                    await asyncio.sleep(Proactive.INTERVAL_MINUTES * 60)
                    continue

                random_knowledge = await get_random_knowledge(Context.KNOWLEDGE_RANDOM)
                active_users = list({u for u, _ in recent_chat[-20:]})
                target_user = random.choice(active_users) if active_users else None

                if target_user and random.random() < 0.5:
                    event_prompt = f'Прокомментируй что-то про @{target_user} на основе того что он писал в чате. Коротко, 1 предложение.'
                else:
                    event_prompt = 'Скажи что-то в чат от себя. Можешь прокомментировать обсуждение или сказать что-то рандомное. 1 предложение.'

                ctx = (
                    ContextBuilder()
                    .add_chat(recent_chat)
                    .add_lines('Язык чата', random_knowledge)
                    .add_raw(event_prompt)
                )
                gen_config = make_gen_config()
                text = await generate(ctx.build(), gen_config)
                if text:
                    text = re.sub(r'\s{2,}', ' ', text).strip()
                    if len(text) > TWITCH_MSG_MAX:
                        text = text[:TWITCH_MSG_MAX - 3] + '...'
                    if random.random() < Caps.PROBABILITY:
                        text = caps_preserve_mentions(text)
                    await self._send_chat_message(text)
                    await save_bot_interaction(self.session_id, '_proactive_', event_prompt, text)
            except Exception:
                logger.exception('Proactive message failed')
            await asyncio.sleep(Proactive.INTERVAL_MINUTES * 60)

    async def _subscribe_to_chat(self) -> None:
        if not self._channel_id:
            logger.error('Failed to get channel ID')
            return
        sub = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._channel_id,
            user_id=str(self.bot_id),
        )
        await self.subscribe_websocket(sub, as_bot=True)
        logger.info('Subscribed to chat #%s', Twitch.CHANNEL)

        try:
            follow_sub = eventsub.ChannelFollowSubscription(
                broadcaster_user_id=self._channel_id,
                moderator_user_id=str(self.bot_id),
            )
            await self.subscribe_websocket(follow_sub, as_bot=True)
            logger.info('Subscribed to follow events')
        except Exception as e:
            logger.warning(
                'Failed to subscribe to follows: %s\n'
                'Re-auth: http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot+moderator:read:followers&force_verify=true',
                e,
            )


class ChatComponent(commands.Component):

    def __init__(self, bot: Bot):
        self.bot = bot
        self._registry = CommandRegistry()
        self._registry.add(HELP_TRIGGER,    self._handle_help)
        self._registry.add(STATS_TRIGGER,   self._handle_stats)
        self._registry.add(SUMMARY_TRIGGER, self._handle_summary)
        self._registry.add(WHO_TRIGGER,     self._handle_who,    prefix=True)
        self._registry.add(VERSUS_TRIGGER,  self._handle_versus, prefix=True)
        self._registry.add(DEFACT_TRIGGER,  self._handle_defact, prefix=True, role='vip_mod_broadcaster')
        self._registry.add(FACT_TRIGGER,    self._handle_fact,   prefix=True, role='vip_mod_broadcaster')
        self._registry.add(ASK_TRIGGER,     self._handle_ask,    prefix=True)

    @commands.Component.listener()
    async def event_message(self, message: twitchio.ChatMessage) -> None:
        if str(message.chatter.id) == str(self.bot.bot_id):
            return

        if not self.bot._bot_name:
            return

        session_id = self.bot.session_id
        await save_chat_message(session_id, message.chatter.name, message.text)

        bot_tag = f'@{self.bot._bot_name}'
        is_mention = bot_tag.lower() in message.text.lower()
        is_sosur = bool(SOSUR_RE.search(message.text))
        reply = getattr(message, 'reply', None)
        is_reply = reply is not None and str(getattr(reply, 'parent_user_id', '')) == str(self.bot.bot_id)

        if not (is_mention or is_sosur or is_reply):
            return

        user = message.chatter.name
        now = time.time()

        if not message.chatter.broadcaster and user in self.bot._cooldowns:
            remaining = self.bot._cooldowns[user] - now
            if remaining > 0:
                await message.respond(
                    f'@{user}, {Cooldown.MESSAGE.format(seconds=int(remaining) + 1)}'
                )
                return

        original_text = message.text
        prompt = message.text.lower()
        prompt = re.sub(re.escape(bot_tag.lower()), '', prompt)
        prompt = SOSUR_RE.sub('', prompt).strip()
        if not prompt:
            prompt = message.text.lower().strip()

        ctx = CommandContext(
            message=message,
            user=user,
            prompt=prompt,
            original_text=original_text,
            session_id=session_id,
            bot=self.bot,
        )

        entry = self._registry.resolve(prompt)
        if entry:
            if entry.role == 'vip_mod_broadcaster':
                chatter = message.chatter
                if not (chatter.vip or chatter.moderator or chatter.broadcaster):
                    await message.respond(f'@{user}, факты доступны только VIP, модераторам и стримеру.')
                    return
            await entry.handler(ctx)
            return

        await self._handle_default(ctx)

    async def _handle_help(self, ctx: CommandContext) -> None:
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.SECONDS
        await ctx.message.respond(
            f'@{ctx.user}: '
            '!fact <факт> — запомнить | '
            '!defact <факт> — забыть | '
            '!ask <вопрос> — ответ по факту | '
            '!stat — статистика | '
            '!summary — саммари чата | '
            '!who <ник> — досье на юзера | '
            '!versus <ник1> <ник2> — баттл'
        )

    async def _handle_stats(self, ctx: CommandContext) -> None:
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.SECONDS
        (msgs, interactions), (total_msgs, total_interactions, total_sessions) = await asyncio.gather(
            get_session_stats(ctx.session_id),
            get_total_stats(),
        )
        await ctx.message.respond(
            f'@{ctx.user}: сессия {ctx.session_id} — '
            f'сообщений: {msgs}, обращений: {interactions} | '
            f'всего за {total_sessions} сессий — '
            f'сообщений: {total_msgs}, обращений: {total_interactions}'
        )

    async def _handle_summary(self, ctx: CommandContext) -> None:
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.COMMAND_SECONDS
        try:
            recent_chat = await get_recent_chat(ctx.session_id, 500)
            if not recent_chat:
                await ctx.message.respond(f'@{ctx.user}, в этой сессии пока нет сообщений.')
                return
            chat_lines = '\n'.join(f'{u}: {m}' for u, m in recent_chat)
            base_prompt = Gemini.get_system_instruction() or ''
            summary_instruction = (
                f'{base_prompt}\n\n'
                '--- РЕЖИМ САММАРИ ---\n'
                'Сейчас ты делаешь саммари чата за сессию. '
                'Сохраняй свой стиль и характер, но при этом саммари должно быть по делу. '
                'Перечисли основные темы, ключевые моменты и кто что обсуждал. '
                'Можешь добавить свои комментарии в своём стиле, но факты должны быть точными. '
                'Plain text, без markdown. Максимум 900 символов.'
            )
            summary_config = types.GenerateContentConfig(
                system_instruction=summary_instruction,
                temperature=1.2,
                safety_settings=SAFETY_OFF,
            )
            text = await generate(
                f'Сделай краткое саммари этого чата стрима (сессия {ctx.session_id}, {len(recent_chat)} сообщений):\n\n{chat_lines}',
                summary_config,
            )
            await self._send_chunked(ctx.message, ctx.user, text, '[summary]', ctx.session_id)
        except Exception:
            logger.exception('Gemini !summary failed for user %s', ctx.user)
            await ctx.message.respond(f'@{ctx.user}, ошибка при генерации саммари.')

    async def _handle_who(self, ctx: CommandContext) -> None:
        who_args = ctx.prompt[len(WHO_TRIGGER):].lstrip(':').strip().split()
        target = who_args[0].lstrip('@') if who_args else ''
        if not target:
            await ctx.message.respond(f'@{ctx.user}, укажи ник: !who <ник>')
            return
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.COMMAND_SECONDS
        try:
            target_facts, target_msgs, target_interactions = await asyncio.gather(
                get_relevant_facts(target, ''),
                get_user_messages(target, Context.WHO_MESSAGES),
                get_user_interactions(target, 10),
            )
            if not target_facts and not target_msgs and not target_interactions:
                await ctx.message.respond(f'@{ctx.user}, не знаю ничего про @{target}.')
                return
            prompt_ctx = (
                ContextBuilder()
                .add_facts(target_facts, f'Факты про {target}')
                .add_user_messages(f'Сообщения {target} в чате', target_msgs)
                .add_interactions(f'Прошлые обращения {target} к боту', target, target_interactions)
                .add_raw(
                    f'{ctx.user} спрашивает: составь досье на @{target}. '
                    f'Опиши его личность, интересы, стиль общения, о чём пишет в чате. '
                    f'Будь конкретным — ссылайся на реальные сообщения и факты. '
                    f'Ужми всё в 1-3 предложения, максимум 400 символов.'
                )
            )
            text = await generate(prompt_ctx.build(), make_gen_config())
            if text:
                text = cleanup_response(text, ctx.user, WHO_VERSUS_MAX)
            if not text:
                await ctx.message.respond(f'@{ctx.user}, не удалось описать @{target}.')
                return
            if is_caps(ctx.original_text) or random.random() < Caps.PROBABILITY:
                text = caps_preserve_mentions(text)
            await ctx.message.respond(f'@{ctx.user}: {text}')
            await save_bot_interaction(ctx.session_id, ctx.user, f'[who] {target}', text)
        except Exception:
            logger.exception('Gemini !who failed for user %s', ctx.user)
            await ctx.message.respond(f'@{ctx.user}, ошибка при генерации.')

    async def _handle_versus(self, ctx: CommandContext) -> None:
        args = ctx.prompt[len(VERSUS_TRIGGER):].lstrip(':').strip().split()
        nicks = list(dict.fromkeys(a.lstrip('@') for a in args if a.lstrip('@')))
        if len(nicks) < 2:
            await ctx.message.respond(f'@{ctx.user}, нужны два разных ника: !versus <ник1> <ник2>')
            return
        nick1, nick2 = nicks[0], nicks[1]
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.COMMAND_SECONDS
        try:
            facts1, msgs1, interactions1, facts2, msgs2, interactions2 = await asyncio.gather(
                get_relevant_facts(nick1, ''),
                get_user_messages(nick1, Context.VERSUS_MESSAGES),
                get_user_interactions(nick1, 10),
                get_relevant_facts(nick2, ''),
                get_user_messages(nick2, Context.VERSUS_MESSAGES),
                get_user_interactions(nick2, 10),
            )
            if not any([facts1, msgs1, interactions1, facts2, msgs2, interactions2]):
                await ctx.message.respond(f'@{ctx.user}, нет данных ни про @{nick1}, ни про @{nick2}.')
                return
            prompt_ctx = ContextBuilder()
            for nick, facts, msgs, ints in [(nick1, facts1, msgs1, interactions1),
                                            (nick2, facts2, msgs2, interactions2)]:
                prompt_ctx.add_facts(facts, f'Факты про {nick}')
                prompt_ctx.add_user_messages(f'Сообщения {nick} в чате', msgs)
                prompt_ctx.add_interactions(f'Прошлые обращения {nick} к боту', nick, ints)
            prompt_ctx.add_raw(
                f'{ctx.user} спрашивает: сравни @{nick1} и @{nick2}. '
                f'Опиши каждого — личность, интересы, стиль общения, о чём пишут в чате. '
                f'Ссылайся на конкретные сообщения и факты. '
                f'Выбери победителя и объясни почему. Максимум 420 символов.'
            )
            text = await generate(prompt_ctx.build(), make_gen_config())
            if text:
                text = cleanup_response(text, ctx.user, WHO_VERSUS_MAX)
            if not text:
                await ctx.message.respond(f'@{ctx.user}, не удалось сравнить.')
                return
            if is_caps(ctx.original_text) or random.random() < Caps.PROBABILITY:
                text = caps_preserve_mentions(text)
            await ctx.message.respond(f'@{ctx.user}: {text}')
            await save_bot_interaction(ctx.session_id, ctx.user, f'[versus] {nick1} vs {nick2}', text)
        except Exception:
            logger.exception('Gemini !versus failed for user %s', ctx.user)
            await ctx.message.respond(f'@{ctx.user}, ошибка при генерации.')

    async def _handle_defact(self, ctx: CommandContext) -> None:
        query = ctx.prompt[len(DEFACT_TRIGGER):].lstrip(':').strip()
        if query:
            result = await delete_fact(ctx.user, query)
            if result is None:
                await ctx.message.respond(f'@{ctx.user}, такого факта нет.')
            elif isinstance(result, list):
                preview = ' | '.join(f[:50] for f in result[:5])
                await ctx.message.respond(f'@{ctx.user}, нашёл {len(result)} фактов, уточни: {preview}')
            else:
                await ctx.message.respond(f'@{ctx.user}, забыл: {result[:80]}')

    async def _handle_fact(self, ctx: CommandContext) -> None:
        fact = ctx.prompt[len(FACT_TRIGGER):].lstrip(':').strip()
        if fact:
            await save_fact(ctx.user, fact)
            await ctx.message.respond(f'@{ctx.user}, запомнил.')

    async def _handle_ask(self, ctx: CommandContext) -> None:
        ask_prompt = ctx.prompt[len(ASK_TRIGGER):].lstrip(':').strip()
        if not ask_prompt:
            return
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.COMMAND_SECONDS
        try:
            ask_config = types.GenerateContentConfig(
                system_instruction=(
                    'Отвечай кратко и по существу, plain text без форматирования. '
                    'ЗАПРЕЩЕНО: markdown, звёздочки, решётки, списки, нумерация, буллеты. '
                    'Пиши сплошным текстом. Максимум 900 символов.'
                ),
                temperature=Gemini.TEMPERATURE,
                safety_settings=SAFETY_OFF,
            )
            text = await generate(ask_prompt, ask_config)
            await self._send_chunked(ctx.message, ctx.user, text, f'[ask] {ask_prompt}', ctx.session_id)
        except Exception:
            logger.exception('Gemini !ask failed for user %s', ctx.user)
            await ctx.message.respond(f'@{ctx.user}, ошибка при генерации ответа.')

    async def _handle_default(self, ctx: CommandContext) -> None:
        self.bot._cooldowns[ctx.user] = time.time() + Cooldown.SECONDS
        facts, recent_chat, context_results, random_knowledge = await asyncio.gather(
            get_relevant_facts(ctx.user, ctx.prompt),
            get_recent_chat(ctx.session_id, Context.CHAT_MESSAGES),
            search_context(ctx.prompt, Context.SEARCH_RESULTS),
            get_random_knowledge(Context.KNOWLEDGE_RANDOM),
        )

        prompt_ctx = (
            ContextBuilder()
            .add_facts(facts)
            .add_chat(recent_chat)
            .add_lines('Контекст канала', context_results)
            .add_lines('Язык чата', random_knowledge)
            .add_prompt(ctx.user, ctx.prompt)
        )

        try:
            gen_config = make_gen_config()
            text = await generate(prompt_ctx.build(), gen_config)
            if not text:
                logger.warning('Empty response for user %s, retrying without knowledge context', ctx.user)
                text = await generate(prompt_ctx.build_without('Язык чата', 'Контекст канала'), gen_config)
            if text:
                text = cleanup_response(text, ctx.user, TWITCH_MSG_MAX)
            if text:
                if is_caps(ctx.original_text) or random.random() < Caps.PROBABILITY:
                    text = caps_preserve_mentions(text)
                await ctx.message.respond(f'@{ctx.user}: {text}')
                await save_bot_interaction(ctx.session_id, ctx.user, ctx.prompt, text)
            else:
                logger.warning('Empty response after retry for user %s, prompt: %s', ctx.user, ctx.prompt[:100])
                await ctx.message.respond(f'@{ctx.user}, не удалось получить ответ.')
        except Exception:
            logger.exception('Gemini generation failed for user %s', ctx.user)
            await ctx.message.respond(f'@{ctx.user}, произошла ошибка при генерации ответа.')

    async def _send_chunked(self, message: twitchio.ChatMessage, user: str,
                            text: str | None, interaction_tag: str,
                            session_id: str) -> None:
        if not text:
            await message.respond(f'@{user}, не удалось получить ответ.')
            return
        text = strip_markdown(text)
        chunks = split_into_chunks(text)
        sent_chunks = []
        for i, chunk in enumerate(chunks):
            try:
                if i == 0:
                    await message.respond(f'@{user}: {chunk}')
                else:
                    await asyncio.sleep(CHUNK_SEND_DELAY)
                    await self.bot._send_chat_message(chunk)
                sent_chunks.append(chunk)
            except Exception:
                logger.exception('Failed to send chunk %d/%d for %s', i + 1, len(chunks), interaction_tag)
        if sent_chunks:
            await save_bot_interaction(session_id, user, interaction_tag, ' '.join(sent_chunks))

    FOLLOW_MESSAGES = [
        '@{user} ЗАЛЕТЕЛ НА КАНАЛ. СОСУРИТИ, ФИКСИРУЕМ ПРОНИКНОВЕНИЕ',
        '@{user} ЗАФИКСИРОВАН В СИСТЕМЕ. ДОБРО ПОЖАЛОВАТЬ В РОДНУЮ ГАВАНЬ',
        '@{user} ТЕПЕРЬ В КИТЕЖ-ГРАДЕ. ОБРАТНОЙ ДОРОГИ НЕТ',
    ]

    @commands.Component.listener()
    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        try:
            user = payload.user.name
            text = random.choice(self.FOLLOW_MESSAGES).format(user=user)
            await payload.respond(text)
            await save_bot_interaction(self.bot.session_id, user, '[follow]', text)
        except Exception:
            logger.exception('event_follow failed')


async def run_bot() -> None:
    validate_config()
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
                logger.info('Shutdown signal received, stopping...')
                bot_task.cancel()
                try:
                    await bot_task
                except asyncio.CancelledError:
                    pass
    finally:
        await close_db()


if __name__ == '__main__':
    from src.cli import main as cli_main
    if not cli_main():
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            pass
