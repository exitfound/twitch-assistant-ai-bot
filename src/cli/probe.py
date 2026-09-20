"""bot.py --probe-context: the same real question under several contexts, side by side.

The owner compares the answers before a
context change is accepted. Nothing is written except what init_db() migrates
on start, like every CLI command.

Questions are real addressings of the bot from chat_messages. Each is asked with
the context as it was at that moment – the stream up to the question, the stream
before it – so the answers are comparable with what the bot could know then.
The memory (profiles, chronicles) is the one exception: it is built over the
whole history and may know what happened later.

Variants:
  now  – what the bot sent before 2026-09-19: the last CONTEXT_CHAT_MESSAGES of the
         session, search, «language», facts; the old fallback on an empty answer
  new  – what the bot sends now: src/gemini/answer_context.py, the same code the
         bot runs – two streams plus memory, and the ladder when Gemini blocks it.
         The rung that answered is printed
"""
import asyncio
import re
import time

from src.core.config import Context
from src.core.content import Content
from src.core.database import (
    get_db, get_random_knowledge, get_recent_chat, get_relevant_facts, search_context,
)
from src.core.utils import SOSUR_RE
from src.gemini import answer_context, client
from src.gemini.context import ContextBuilder

VARIANTS = ('now', 'new')
_LEADING_MENTION = re.compile(r'^\s*@\w+[,:]?\s*')


class _Question:
    def __init__(self, row: tuple) -> None:
        self.id, self.session, self.user, self.text = row
        # What the dispatcher hands the model: the trigger word or @mention stripped
        # the dispatcher lowercases it too
        text = self.text.lower()
        self.prompt = SOSUR_RE.sub('', _LEADING_MENTION.sub('', text), count=1).strip(' ,:') or text


async def _questions(ids: list[int], limit: int) -> list[_Question]:
    """The given messages, or the latest free-text addressings of the bot."""
    db = await get_db()
    if ids:
        marks = ','.join('?' * len(ids))
        sql = (f'SELECT id, session_id, username, message FROM chat_messages'
               f' WHERE id IN ({marks}) ORDER BY id')
        params: tuple = tuple(ids)
    else:
        # Commands are answered by their own handlers, not by this context
        sql = ("SELECT id, session_id, username, message FROM chat_messages"
               " WHERE addressed = 1 AND message NOT LIKE '%!%' AND length(message) >= 20"
               ' ORDER BY id DESC LIMIT ?')
        params = (limit,)
    async with db.execute(sql, params) as cursor:
        return [_Question(row) for row in await cursor.fetchall()]


async def _now(q: _Question, config) -> tuple[str | None, str]:
    """The answer as handle_default gave it before 2026-09-19."""
    facts, chat, found, language = await asyncio.gather(
        get_relevant_facts(q.user, q.prompt),
        get_recent_chat(q.session, Context.CHAT_MESSAGES, q.id),
        search_context(q.prompt, Context.SEARCH_RESULTS),
        get_random_knowledge(Context.KNOWLEDGE_RANDOM),
    )
    builder = (ContextBuilder().add_facts(Content.label('facts'), facts)
               .add_chat(Content.label('chat'), chat)
               .add_lines(Content.label('channel'), found)
               .add_lines(Content.label('language'), language)
               .add_raw(Content.prompt('user_question', user=q.user, prompt=q.prompt)))
    text = await client.generate(builder.build(), config)
    if text:
        return text, 'как было'
    text = await client.generate(
        builder.build_without(Content.label('language'), Content.label('channel')), config,
    )
    return text, 'как было, повтор'


async def _new(q: _Question, config) -> tuple[str | None, str]:
    return await answer_context.answer(
        # History before 2026-09-17 has date sessions only, yet those were streams
        answer_context.Question(q.session, q.user, q.prompt, before_id=q.id, stream=True), config,
    )


async def _answer(variant: str, q: _Question) -> tuple[str | None, str, int, float]:
    """Answer, rung, input tokens (every try, blocked ones too) and seconds.
    One call at a time, so the token counter is exact."""
    before = client.usage['prompt']
    started = time.monotonic()
    run = _now if variant == 'now' else _new
    text, rung = await run(q, client.make_gen_config())
    return text, rung, client.usage['prompt'] - before, time.monotonic() - started


async def probe(ids: list[int], limit: int, samples: int) -> None:
    questions = await _questions(ids, limit)
    if not questions:
        print('Вопросов не найдено')
        return
    print(f'Вопросов: {len(questions)}, вариантов: {len(VARIANTS)}, ответов на вариант: {samples}. '
          f'Температура и персона – как у бота. Чат – строго до вопроса; профили и хроники '
          f'знают всю историю, в том числе то, что было после.')
    totals = {v: [0, 0.0, 0] for v in VARIANTS}
    for q in questions:
        print(f'\n{"#" * 70}\n[{q.id}] {q.session} | {q.user}: {q.text}')
        for variant in VARIANTS:
            for _ in range(samples):
                text, rung, tokens, seconds = await _answer(variant, q)
                totals[variant][0] += tokens
                totals[variant][1] += seconds
                totals[variant][2] += 1
                print(f'\n  --- {variant} [{rung}] ({tokens:,} ток., {seconds:.1f} с)'.replace(',', ' '))
                print(f'  {text or "(пусто)"}')

    print(f'\n{"=" * 70}\nВ среднем на ответ:')
    for variant, (tokens, seconds, n) in totals.items():
        dollars = tokens / n * 0.30 / 1_000_000
        print(f'  {variant:8} {tokens // n:>7,} ток. входа  {seconds / n:4.1f} с  ~${dollars:.4f}'.replace(',', ' '))
    u = client.usage
    total = (u['prompt'] * 0.30 + u['output'] * 2.50) / 1_000_000
    print(f'Весь прогон: ~${total:.2f}')
