import asyncio
import logging
import random
import re
import signal
import time
import twitchio
from google import genai
from twitchio import eventsub
from twitchio.ext import commands
from google.genai import types

from src.config import Twitch, Gemini, Cooldown, Context, Caps, Proactive
from src.database import (
    init_db, close_db, save_chat_message, save_bot_interaction, save_fact, delete_fact,
    get_relevant_facts, get_recent_chat, search_context,
    get_random_knowledge, get_session_stats, get_total_stats,
    get_user_messages, get_user_interactions,
)
from src.utils import is_caps

logger = logging.getLogger(__name__)

_genai_client = None


def get_genai_client():
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=Gemini.API_KEY)
    return _genai_client

FACT_TRIGGER = '!fact'
DEFACT_TRIGGER = '!defact'
STATS_TRIGGER = '!stat'
HELP_TRIGGER = '!help'
ASK_TRIGGER = '!ask'
SUMMARY_TRIGGER = '!summary'
WHO_TRIGGER = '!who'
VERSUS_TRIGGER = '!versus'
SOSUR_RE = re.compile(r'сосур\w*', re.IGNORECASE | re.UNICODE)
MENTION_RE = re.compile(r'@\S+')


def _caps_preserve_mentions(text: str) -> str:
    parts = MENTION_RE.split(text)
    mentions = MENTION_RE.findall(text)
    result = []
    for i, part in enumerate(parts):
        result.append(part.upper())
        if i < len(mentions):
            result.append(mentions[i])
    return ''.join(result)


def _make_gen_config():
    config = types.GenerateContentConfig(
        system_instruction=Gemini.get_system_instruction(),
        temperature=Gemini.TEMPERATURE,
        safety_settings=[
            types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='OFF'),
            types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='OFF'),
            types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='OFF'),
            types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='OFF'),
            types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='OFF'),
        ],
    )
    if Gemini.THINKING_BUDGET >= 0:
        config.thinking_config = types.ThinkingConfig(
            thinking_budget=Gemini.THINKING_BUDGET,
        )
    return config


class Bot(commands.Bot):

    def __init__(self):
        super().__init__(
            client_id=Twitch.CLIENT_ID,
            client_secret=Twitch.CLIENT_SECRET,
            bot_id=Twitch.BOT_ID,
            prefix='!',
        )
        self._cooldowns = {}
        self._bot_name = None
        self._channel_id = None

    @property
    def session_id(self):
        return time.strftime('%Y-%m-%d')

    async def setup_hook(self):
        await init_db()
        if Twitch.BOT_TOKEN and Twitch.BOT_REFRESH:
            await self.add_token(Twitch.BOT_TOKEN, Twitch.BOT_REFRESH)
        users = await self.fetch_users(logins=[Twitch.CHANNEL])
        if users:
            self._channel_id = str(users[0].id)
        await self.add_component(ChatComponent(self))

    async def event_ready(self):
        users = await self.fetch_users(ids=[self.bot_id])
        if users:
            self._bot_name = users[0].name
        print(f'\nBot started | Username: {self._bot_name or self.bot_id} | Session: {self.session_id}\n')
        try:
            await self._subscribe_to_chat()
        except Exception as e:
            logger.warning('Failed to subscribe to chat: %s', e)
            print(
                '\nNo token found. Open in browser and log in as the bot account:\n'
                'http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true\n'
            )
        if Proactive.ENABLED and self._channel_id:
            self._proactive_task = asyncio.create_task(self._proactive_loop())
            print(f'Proactive messages enabled (every {Proactive.INTERVAL_MINUTES} min)')

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

    async def _send_chat_message(self, text: str):
        await self._http.post_chat_message(
            broadcaster_id=self._channel_id,
            sender_id=str(self.bot_id),
            message=text,
            token_for=str(self.bot_id),
        )

    async def _proactive_loop(self):
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

                parts = []
                if recent_chat:
                    lines = '\n'.join(f'{u}: {m}' for u, m in recent_chat)
                    parts.append(f'[Последние сообщения в чате]\n{lines}')
                if random_knowledge:
                    lines = '\n'.join(random_knowledge)
                    parts.append(f'[Язык чата]\n{lines}')
                parts.append(event_prompt)

                gen_config = _make_gen_config()
                response = await get_genai_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents='\n\n'.join(parts),
                    config=gen_config,
                )
                text = None
                try:
                    text = response.text
                except Exception:
                    pass
                if text:
                    text = re.sub(r'\s{2,}', ' ', text).strip()
                    if len(text) > 450:
                        text = text[:447] + '...'
                    if random.random() < Caps.PROBABILITY:
                        text = _caps_preserve_mentions(text)
                    await self._send_chat_message(text)
                    await save_bot_interaction(self.session_id, '_proactive_', event_prompt, text)
            except Exception:
                logger.exception('Proactive message failed')
            await asyncio.sleep(Proactive.INTERVAL_MINUTES * 60)

    async def _subscribe_to_chat(self):
        if not self._channel_id:
            print('\nError: failed to get channel ID\n')
            return
        sub = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._channel_id,
            user_id=str(self.bot_id),
        )
        await self.subscribe_websocket(sub, as_bot=True)
        print(f'Subscribed to chat #{Twitch.CHANNEL}')

        # Follow events (bot is moderator — has moderator:read:followers)
        try:
            follow_sub = eventsub.ChannelFollowSubscription(
                broadcaster_user_id=self._channel_id,
                moderator_user_id=str(self.bot_id),
            )
            await self.subscribe_websocket(follow_sub, as_bot=True)
            print('Subscribed to follow events')
        except Exception as e:
            logger.warning('Failed to subscribe to follows: %s', e)
            print(
                '\nFollow events require moderator:read:followers scope.\n'
                'Re-auth the bot with:\n'
                'http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot+moderator:read:followers&force_verify=true\n'
            )
        print()


class ChatComponent(commands.Component):

    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.Component.listener()
    async def event_message(self, message: twitchio.ChatMessage):
        if str(message.chatter.id) == str(self.bot.bot_id):
            return

        if not self.bot._bot_name:
            return

        await save_chat_message(self.bot.session_id, message.chatter.name, message.text)

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

        if prompt == HELP_TRIGGER:
            await message.respond(
                f'@{user}: '
                '!fact <факт> — запомнить | '
                '!defact <факт> — забыть | '
                '!ask <вопрос> — ответ по факту | '
                '!stat — статистика | '
                '!summary — саммари чата | '
                '!who <ник> — досье на юзера | '
                '!versus <ник1> <ник2> — баттл'
            )
            return

        if prompt == STATS_TRIGGER:
            (msgs, interactions), (total_msgs, total_interactions, total_sessions) = await asyncio.gather(
                get_session_stats(self.bot.session_id),
                get_total_stats(),
            )
            await message.respond(
                f'@{user}: сессия {self.bot.session_id} — '
                f'сообщений: {msgs}, обращений: {interactions} | '
                f'всего за {total_sessions} сессий — '
                f'сообщений: {total_msgs}, обращений: {total_interactions}'
            )
            return

        if prompt == SUMMARY_TRIGGER:
            self.bot._cooldowns[user] = time.time() + Cooldown.COMMAND_SECONDS
            try:
                recent_chat = await get_recent_chat(self.bot.session_id, 500)
                if not recent_chat:
                    await message.respond(f'@{user}, в этой сессии пока нет сообщений.')
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
                    safety_settings=[
                        types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='OFF'),
                        types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='OFF'),
                        types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='OFF'),
                        types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='OFF'),
                        types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='OFF'),
                    ],
                )
                response = await get_genai_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents=f'Сделай краткое саммари этого чата стрима (сессия {self.bot.session_id}, {len(recent_chat)} сообщений):\n\n{chat_lines}',
                    config=summary_config,
                )
                text = None
                try:
                    text = response.text
                except Exception:
                    pass
                if text:
                    text = re.sub(r'\*+', '', text)
                    text = re.sub(r'#+\s*', '', text)
                    text = re.sub(r'[_`~>|]', '', text)
                    text = re.sub(r'^\s*[-•●]\s+', '', text, flags=re.MULTILINE)
                    text = re.sub(r'^\s*\d+[\.\)]\s+', '', text, flags=re.MULTILINE)
                    text = re.sub(r'\n', ' ', text)
                    text = re.sub(r'\s{2,}', ' ', text).strip()
                    max_total = 450 * 3
                    if len(text) > max_total:
                        text = text[:max_total - 3] + '...'
                    chunks = []
                    while text:
                        if len(text) <= 450:
                            chunks.append(text)
                            break
                        cut = text.rfind(' ', 0, 450)
                        if cut <= 0:
                            cut = 450
                        chunks.append(text[:cut])
                        text = text[cut:].lstrip()
                    for i, chunk in enumerate(chunks):
                        try:
                            if i == 0:
                                await message.respond(f'@{user}: {chunk}')
                            else:
                                await asyncio.sleep(1.5)
                                await self.bot._send_chat_message(chunk)
                        except Exception:
                            logger.exception('!summary: failed to send chunk %d/%d', i + 1, len(chunks))
                    await save_bot_interaction(self.bot.session_id, user, '[summary]', ' '.join(chunks))
                else:
                    await message.respond(f'@{user}, не удалось сгенерировать саммари.')
            except Exception:
                logger.exception('Gemini !summary failed for user %s', user)
                await message.respond(f'@{user}, ошибка при генерации саммари.')
            return

        if prompt.startswith(WHO_TRIGGER):
            who_args = prompt[len(WHO_TRIGGER):].lstrip(':').strip().split()
            target = who_args[0].lstrip('@').lower() if who_args else ''
            if not target:
                await message.respond(f'@{user}, укажи ник: !who <ник>')
                return
            self.bot._cooldowns[user] = time.time() + Cooldown.COMMAND_SECONDS
            try:
                target_facts, target_msgs, target_interactions = await asyncio.gather(
                    get_relevant_facts(target, ''),
                    get_user_messages(target, Context.WHO_MESSAGES),
                    get_user_interactions(target, 10),
                )
                if not target_facts and not target_msgs and not target_interactions:
                    await message.respond(f'@{user}, не знаю ничего про @{target}.')
                    return
                parts = []
                if target_facts:
                    lines = '\n'.join(f'{u}: {f}' for u, f in target_facts)
                    parts.append(f'[Факты про {target}]\n{lines}')
                if target_msgs:
                    lines = '\n'.join(target_msgs)
                    parts.append(f'[Сообщения {target} в чате]\n{lines}')
                if target_interactions:
                    lines = '\n'.join(f'{target}: {q} → бот: {a}' for q, a in target_interactions)
                    parts.append(f'[Прошлые обращения {target} к боту]\n{lines}')
                parts.append(
                    f'{user} спрашивает: составь досье на @{target}. '
                    f'Опиши его личность, интересы, стиль общения, о чём пишет в чате. '
                    f'Будь конкретным — ссылайся на реальные сообщения и факты. '
                    f'Ужми всё в 1-3 предложения, максимум 400 символов.'
                )
                gen_config = _make_gen_config()
                response = await get_genai_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents='\n\n'.join(parts),
                    config=gen_config,
                )
                text = None
                try:
                    text = response.text
                except Exception:
                    pass
                if text:
                    if text.lower().startswith(f'@{user}'):
                        text = text[len(f'@{user}'):].lstrip(':,').strip()
                    text = re.sub(re.escape(f'@{user}'), '', text, flags=re.IGNORECASE).strip()
                    text = re.sub(r'\s{2,}', ' ', text)
                    if len(text) > 420:
                        text = text[:417] + '...'
                    if is_caps(original_text) or random.random() < Caps.PROBABILITY:
                        text = _caps_preserve_mentions(text)
                    await message.respond(f'@{user}: {text}')
                    await save_bot_interaction(self.bot.session_id, user, f'[who] {target}', text)
                else:
                    await message.respond(f'@{user}, не удалось описать @{target}.')
            except Exception:
                logger.exception('Gemini !who failed for user %s', user)
                await message.respond(f'@{user}, ошибка при генерации.')
            return

        if prompt.startswith(VERSUS_TRIGGER):
            args = prompt[len(VERSUS_TRIGGER):].lstrip(':').strip().split()
            nicks = list(dict.fromkeys(a.lstrip('@').lower() for a in args if a.lstrip('@')))
            if len(nicks) < 2:
                await message.respond(f'@{user}, нужны два разных ника: !versus <ник1> <ник2>')
                return
            nick1, nick2 = nicks[0], nicks[1]
            self.bot._cooldowns[user] = time.time() + Cooldown.COMMAND_SECONDS
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
                    await message.respond(f'@{user}, нет данных ни про @{nick1}, ни про @{nick2}.')
                    return
                parts = []
                if facts1:
                    lines = '\n'.join(f'{u}: {f}' for u, f in facts1)
                    parts.append(f'[Факты про {nick1}]\n{lines}')
                if msgs1:
                    lines = '\n'.join(msgs1)
                    parts.append(f'[Сообщения {nick1} в чате]\n{lines}')
                if interactions1:
                    lines = '\n'.join(f'{nick1}: {q} → бот: {a}' for q, a in interactions1)
                    parts.append(f'[Прошлые обращения {nick1} к боту]\n{lines}')
                if facts2:
                    lines = '\n'.join(f'{u}: {f}' for u, f in facts2)
                    parts.append(f'[Факты про {nick2}]\n{lines}')
                if msgs2:
                    lines = '\n'.join(msgs2)
                    parts.append(f'[Сообщения {nick2} в чате]\n{lines}')
                if interactions2:
                    lines = '\n'.join(f'{nick2}: {q} → бот: {a}' for q, a in interactions2)
                    parts.append(f'[Прошлые обращения {nick2} к боту]\n{lines}')
                parts.append(
                    f'{user} спрашивает: сравни @{nick1} и @{nick2}. '
                    f'Опиши каждого — личность, интересы, стиль общения, о чём пишут в чате. '
                    f'Ссылайся на конкретные сообщения и факты. '
                    f'Выбери победителя и объясни почему. Максимум 420 символов.'
                )
                gen_config = _make_gen_config()
                response = await get_genai_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents='\n\n'.join(parts),
                    config=gen_config,
                )
                text = None
                try:
                    text = response.text
                except Exception:
                    pass
                if text:
                    if text.lower().startswith(f'@{user}'):
                        text = text[len(f'@{user}'):].lstrip(':,').strip()
                    text = re.sub(re.escape(f'@{user}'), '', text, flags=re.IGNORECASE).strip()
                    text = re.sub(r'\s{2,}', ' ', text)
                    if len(text) > 420:
                        text = text[:417] + '...'
                    if is_caps(original_text) or random.random() < Caps.PROBABILITY:
                        text = _caps_preserve_mentions(text)
                    await message.respond(f'@{user}: {text}')
                    await save_bot_interaction(self.bot.session_id, user, f'[versus] {nick1} vs {nick2}', text)
                else:
                    await message.respond(f'@{user}, не удалось сравнить.')
            except Exception:
                logger.exception('Gemini !versus failed for user %s', user)
                await message.respond(f'@{user}, ошибка при генерации.')
            return

        if prompt.startswith(DEFACT_TRIGGER) or prompt.startswith(FACT_TRIGGER):
            chatter = message.chatter
            if not (chatter.vip or chatter.moderator or chatter.broadcaster):
                await message.respond(f'@{user}, факты доступны только VIP, модераторам и стримеру.')
                return

        if prompt.startswith(DEFACT_TRIGGER):
            query = prompt[len(DEFACT_TRIGGER):].lstrip(':').strip()
            if query:
                result = await delete_fact(user, query)
                if result is None:
                    await message.respond(f'@{user}, такого факта нет.')
                elif isinstance(result, list):
                    preview = ' | '.join(f[:50] for f in result[:5])
                    await message.respond(f'@{user}, нашёл {len(result)} фактов, уточни: {preview}')
                else:
                    await message.respond(f'@{user}, забыл: {result[:80]}')
                return

        if prompt.startswith(FACT_TRIGGER):
            fact = prompt[len(FACT_TRIGGER):].lstrip(':').strip()
            if fact:
                await save_fact(user, fact)
                await message.respond(f'@{user}, запомнил.')
                return

        if prompt.startswith(ASK_TRIGGER):
            ask_prompt = prompt[len(ASK_TRIGGER):].lstrip(':').strip()
            if ask_prompt:
                self.bot._cooldowns[user] = time.time() + Cooldown.COMMAND_SECONDS
                try:
                    ask_config = types.GenerateContentConfig(
                        system_instruction=(
                            'Отвечай кратко и по существу, plain text без форматирования. '
                            'ЗАПРЕЩЕНО: markdown, звёздочки, решётки, списки, нумерация, буллеты. '
                            'Пиши сплошным текстом. Максимум 900 символов.'
                        ),
                        temperature=Gemini.TEMPERATURE,
                        safety_settings=[
                            types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='OFF'),
                            types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='OFF'),
                            types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='OFF'),
                            types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='OFF'),
                            types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='OFF'),
                        ],
                    )
                    response = await get_genai_client().aio.models.generate_content(
                        model=Gemini.MODEL,
                        contents=ask_prompt,
                        config=ask_config,
                    )
                    text = None
                    try:
                        text = response.text
                    except Exception:
                        pass
                    if text:
                        text = re.sub(r'\*+', '', text)
                        text = re.sub(r'#+\s*', '', text)
                        text = re.sub(r'[_`~>|]', '', text)
                        text = re.sub(r'^\s*[-•●]\s+', '', text, flags=re.MULTILINE)
                        text = re.sub(r'^\s*\d+[\.\)]\s+', '', text, flags=re.MULTILINE)
                        text = re.sub(r'\n', ' ', text)
                        text = re.sub(r'\s{2,}', ' ', text).strip()
                        max_total = 450 * 3
                        if len(text) > max_total:
                            text = text[:max_total - 3] + '...'
                        chunks = []
                        while text:
                            if len(text) <= 450:
                                chunks.append(text)
                                break
                            cut = text.rfind(' ', 0, 450)
                            if cut <= 0:
                                cut = 450
                            chunks.append(text[:cut])
                            text = text[cut:].lstrip()
                        logger.info('!ask: %d chunks to send for user %s', len(chunks), user)
                        for i, chunk in enumerate(chunks):
                            try:
                                if i == 0:
                                    await message.respond(f'@{user}: {chunk}')
                                else:
                                    await asyncio.sleep(1.5)
                                    await self.bot._send_chat_message(chunk)
                                logger.info('!ask: sent chunk %d/%d (%d chars)', i + 1, len(chunks), len(chunk))
                            except Exception:
                                logger.exception('!ask: failed to send chunk %d/%d', i + 1, len(chunks))
                        await save_bot_interaction(self.bot.session_id, user, f'[ask] {ask_prompt}', ' '.join(chunks))
                    else:
                        await message.respond(f'@{user}, не удалось получить ответ.')
                except Exception:
                    logger.exception('Gemini !ask failed for user %s', user)
                    await message.respond(f'@{user}, ошибка при генерации ответа.')
                return

        facts, recent_chat, context_results, random_knowledge = await asyncio.gather(
            get_relevant_facts(user, prompt),
            get_recent_chat(self.bot.session_id, Context.CHAT_MESSAGES),
            search_context(prompt, Context.SEARCH_RESULTS),
            get_random_knowledge(Context.KNOWLEDGE_RANDOM),
        )

        context_parts = []
        if facts:
            lines = '\n'.join(f'{u}: {f}' for u, f in facts)
            context_parts.append(f'[Сохранённые факты]\n{lines}')
        if recent_chat:
            lines = '\n'.join(f'{u}: {m}' for u, m in recent_chat)
            context_parts.append(f'[Последние сообщения в чате]\n{lines}')
        if context_results:
            lines = '\n'.join(context_results)
            context_parts.append(f'[Контекст канала]\n{lines}')
        if random_knowledge:
            lines = '\n'.join(random_knowledge)
            context_parts.append(f'[Язык чата]\n{lines}')
        context_parts.append(f'{user} спрашивает: {prompt}')
        full_prompt = '\n\n'.join(context_parts)

        self.bot._cooldowns[user] = time.time() + Cooldown.SECONDS
        try:
            gen_config = _make_gen_config()
            response = await get_genai_client().aio.models.generate_content(
                model=Gemini.MODEL,
                contents=full_prompt,
                config=gen_config,
            )
            text = None
            try:
                text = response.text
            except Exception:
                pass
            if not text:
                logger.warning('Empty response for user %s, retrying without knowledge context', user)
                fallback_parts = [p for p in context_parts if not p.startswith('[Язык чата]') and not p.startswith('[Контекст канала]')]
                fallback_prompt = '\n\n'.join(fallback_parts)
                response = await get_genai_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents=fallback_prompt,
                    config=gen_config,
                )
                try:
                    text = response.text
                except Exception:
                    pass
            if text:
                if text.lower().startswith(f'@{user}'):
                    text = text[len(f'@{user}'):].lstrip(':,').strip()
                text = re.sub(re.escape(f'@{user}'), '', text, flags=re.IGNORECASE).strip()
                text = re.sub(r'\s{2,}', ' ', text)
            if text and len(text) > 450:
                text = text[:447] + '...'
            if text:
                if is_caps(original_text) or random.random() < Caps.PROBABILITY:
                    text = _caps_preserve_mentions(text)
                await message.respond(f'@{user}: {text}')
                await save_bot_interaction(self.bot.session_id, user, prompt, text)
            else:
                logger.warning('Empty response after retry for user %s, prompt: %s', user, prompt[:100])
                await message.respond(f'@{user}, не удалось получить ответ.')
        except Exception:
            logger.exception('Gemini generation failed for user %s', user)
            await message.respond(f'@{user}, произошла ошибка при генерации ответа.')


    FOLLOW_MESSAGES = [
        '@{user} ЗАЛЕТЕЛ НА КАНАЛ. СОСУРИТИ, ФИКСИРУЕМ ПРОНИКНОВЕНИЕ',
        '@{user} ЗАФИКСИРОВАН В СИСТЕМЕ. ДОБРО ПОЖАЛОВАТЬ В РОДНУЮ ГАВАНЬ',
        '@{user} ТЕПЕРЬ В КИТЕЖ-ГРАДЕ. ОБРАТНОЙ ДОРОГИ НЕТ',
    ]

    @commands.Component.listener()
    async def event_follow(self, payload: twitchio.ChannelFollow):
        user = payload.user.name
        text = random.choice(self.FOLLOW_MESSAGES).format(user=user)
        await payload.respond(text)
        await save_bot_interaction(self.bot.session_id, user, '[follow]', text)


async def run_bot():
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: loop.stop())
    try:
        async with Bot() as bot:
            await bot.start()
    finally:
        await close_db()


async def upload_lore(files: list[str], clear: bool, dry_run: bool):
    from src.knowledge import parse_lore_file, dedup_entries, clear_knowledge, import_entries

    all_entries = []
    for path in files:
        entries = parse_lore_file(path)
        print(f'{path}: {len(entries)} записей')
        all_entries.extend(entries)

    unique = dedup_entries(all_entries)
    dupes = len(all_entries) - len(unique)
    if dupes:
        print(f'Дубликатов между файлами: {dupes}')

    if dry_run:
        print(f'\n--- Dry run: {len(unique)} уникальных записей ---')
        for i, entry in enumerate(unique[:20], 1):
            print(f'  {i}. {entry[:100]}{"..." if len(entry) > 100 else ""}')
        if len(unique) > 20:
            print(f'  ... и ещё {len(unique) - 20}')
        return

    await init_db()
    try:
        if clear:
            await clear_knowledge()
            print('База знаний очищена (knowledge + knowledge_fts)')
        if unique:
            added, skipped = await import_entries(unique)
            print(f'Импортировано: {added}, пропущено дублей в БД: {skipped}')
    finally:
        await close_db()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Twitch AI Bot')
    parser.add_argument(
        '--upload-lore', nargs='+', metavar='FILE',
        help='Импорт лора из txt файлов (бот не запускается)',
    )
    parser.add_argument(
        '--clear-lore', action='store_true',
        help='Очистить базу знаний (с --upload-lore: перед импортом, без: только очистка)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Показать что будет импортировано (без записи в БД)',
    )
    parser.add_argument(
        '--list-facts', action='store_true',
        help='Показать все сохранённые факты из БД',
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    if args.list_facts:
        async def _list_facts():
            await init_db()
            try:
                from src.database import get_db
                db = await get_db()
                async with db.execute(
                    'SELECT username, fact, created_at FROM facts ORDER BY username, id'
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    print('Фактов нет.')
                    return
                current_user = None
                for username, fact, created_at in rows:
                    if username != current_user:
                        current_user = username
                        print(f'\n  @{username}:')
                    print(f'    - {fact}  ({created_at})')
                print(f'\nВсего: {len(rows)} фактов')
            finally:
                await close_db()
        asyncio.run(_list_facts())
    elif args.upload_lore or args.clear_lore:
        if args.clear_lore and not args.upload_lore:
            async def _clear():
                from src.knowledge import clear_knowledge
                await init_db()
                try:
                    await clear_knowledge()
                    print('База знаний очищена (knowledge + knowledge_fts)')
                finally:
                    await close_db()
            asyncio.run(_clear())
        else:
            asyncio.run(upload_lore(args.upload_lore, args.clear_lore, args.dry_run))
    else:
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            pass
