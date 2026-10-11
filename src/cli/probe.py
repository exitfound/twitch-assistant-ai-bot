"""bot.py --probe-context: the same real question under several contexts, side by side.

For comparing answers before a context change is accepted. Nothing is written except
what init_db() migrates on start, like every CLI command.

Questions are real addressings of the bot from chat_messages, each asked with the
context as it was at that moment – the chat up to the question, and a pulse built
from it on the spot – so the answers are comparable with what the bot could know then.
The memory (dossiers, chronicles) is the exception: it is built over the whole history.

Variants:
  now  – the narrow context: the session's last CONTEXT_CHAT_MESSAGES, search,
         «language», facts, with the plain fallback on an empty answer
  new  – src/gemini/answer_context.py, the same code the bot runs: the pulse, the thread,
         the dossiers, and the ladder when Gemini blocks it. The rung that answered is printed

With a Candidate (--temperature, --thinking, --system, --model) the context stays the
bot's and the generation settings are compared instead:
  bot        – the bot's own temperature, thinking budget, persona and model
  candidate  – the same context with the given settings, the rest as the bot's
"""
import asyncio
import re
import time
from dataclasses import dataclass

from src.core.config import Context
from src.core.content import Content
from src.core.database import (
    get_db, get_random_knowledge, get_recent_chat, get_relevant_facts, search_context,
)
from src.core.utils import SOSUR_RE
from src.gemini import answer_context, client, pulse
from src.gemini.context import ContextBuilder

VARIANTS = ('now', 'new')
CANDIDATE_VARIANTS = ('bot', 'candidate')
_LEADING_MENTION = re.compile(r'^\s*@\w+[,:]?\s*')


class _Question:
    def __init__(self, row: tuple) -> None:
        self.id, self.session, self.user, self.text = row
        # What the dispatcher hands the model: the trigger word or @mention stripped,
        # the case kept
        text = self.text
        self.prompt = SOSUR_RE.sub('', _LEADING_MENTION.sub('', text), count=1).strip(' ,:') or text
        # Built once per question before the variants, so they all see the same one
        self.pulse: str | None = None


async def _questions(ids: list[int], limit: int) -> list[_Question]:
    """The given messages, or the latest free-text addressings of the bot."""
    db = await get_db()
    if ids:
        marks = ','.join('?' * len(ids))
        sql = (f'SELECT id, session_id, username, message FROM chat_messages'
               f' WHERE id IN ({marks}) ORDER BY id')
        params: tuple = tuple(ids)
    else:
        # Commands are answered by their own handlers, not by this context: a `!` at the
        # start or after a space («сосурян !who ник»). One inside a sentence is a question
        sql = ("SELECT id, session_id, username, message FROM chat_messages"
               " WHERE addressed = 1 AND message NOT LIKE '!%' AND message NOT LIKE '% !%'"
               " AND length(message) >= 20"
               ' ORDER BY id DESC LIMIT ?')
        params = (limit,)
    async with db.execute(sql, params) as cursor:
        return [_Question(row) for row in await cursor.fetchall()]


async def _now(q: _Question, config) -> tuple[str | None, str]:
    """The answer on the narrow context, without the ladder."""
    facts, chat, found, language = await asyncio.gather(
        get_relevant_facts(q.user, q.prompt),
        get_recent_chat(q.session, Context.CHAT_MESSAGES, q.id),
        search_context(q.prompt, Context.SEARCH_RESULTS),
        get_random_knowledge(Context.KNOWLEDGE_RANDOM),
    )
    builder = (ContextBuilder().add_pairs(Content.label('facts'), facts)
               .add_pairs(Content.label('chat'), chat)
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


@dataclass
class Candidate:
    """Generation settings tried next to the bot's own; None keeps the bot's value."""
    temperature: float | None = None
    thinking: int | None = None
    system: str | None = None
    model: str | None = None

    def config(self):
        return client.make_gen_config(system=self.system, temperature=self.temperature,
                                      thinking_budget=self.thinking)

    def describe(self) -> str:
        parts = [f'{name} {value}' for name, value in (
            ('температура', self.temperature), ('thinking', self.thinking), ('модель', self.model),
        ) if value is not None]
        if self.system is not None:
            parts.append('своя персона')
        return ', '.join(parts)


async def _new(q: _Question, config) -> tuple[str | None, str]:
    return await answer_context.answer(
        # The older history has date-shaped session ids, though those sessions are streams
        answer_context.Question(q.session, q.user, q.prompt, before_id=q.id, stream=True, pulse=q.pulse),
        config,
    )


async def _answer(variant: str, q: _Question,
                  candidate: Candidate | None) -> tuple[str | None, str, int, int, float]:
    """Answer, rung, input tokens (every try, blocked ones too), output tokens with the
    thinking ones, and seconds. One call at a time, so the token counters are exact."""
    before, before_out = client.usage['prompt'], client.usage['output']
    started = time.monotonic()
    if variant == 'candidate':
        with client.using_model(candidate.model):
            text, rung = await _new(q, candidate.config())
    else:
        run = _now if variant == 'now' else _new
        text, rung = await run(q, client.make_gen_config())
    return (text, rung, client.usage['prompt'] - before, client.usage['output'] - before_out,
            time.monotonic() - started)


async def probe(ids: list[int], limit: int, samples: int, candidate: Candidate | None = None) -> None:
    questions = await _questions(ids, limit)
    if not questions:
        print('Вопросов не найдено')
        return
    variants = CANDIDATE_VARIANTS if candidate else VARIANTS
    settings = (f'candidate: {candidate.describe()}, остальное как у бота'
                if candidate else 'Температура и персона – как у бота')
    print(f'Вопросов: {len(questions)}, вариантов: {len(variants)}, ответов на вариант: {samples}. '
          f'{settings}. Чат – строго до вопроса; профили и хроники '
          f'знают всю историю, в том числе то, что было после.')
    totals = {v: [0, 0, 0.0, 0] for v in variants}
    for q in questions:
        print(f'\n{"#" * 70}\n[{q.id}] {q.session} | {q.user}: {q.text}')
        if any(v != 'now' for v in variants):
            q.pulse = await pulse.build(q.session, q.id)
            print('  пульс: ' + (q.pulse.replace('\n', ' | ') if q.pulse else '(нет)'))
        for variant in variants:
            for _ in range(samples):
                text, rung, tokens, output, seconds = await _answer(variant, q, candidate)
                total = totals[variant]
                total[0] += tokens
                total[1] += output
                total[2] += seconds
                total[3] += 1
                print(f'\n  --- {variant} [{rung}] ({tokens:,} ток., {output:,} вых., {seconds:.1f} с)'
                      .replace(',', ' '))
                print(f'  {text or "(пусто)"}')

    print(f'\n{"=" * 70}\nВ среднем на ответ:')
    run_total: float | None = 0.0
    for variant, (tokens, output, seconds, n) in totals.items():
        model = candidate.model if variant == 'candidate' and candidate else None
        each = client.cost_estimate(tokens // n, output // n, model)
        print(f'  {variant:9} {tokens // n:>7,} ток. входа  {output // n:>5,} вых.  {seconds / n:4.1f} с'
              f'  {client.cost_text(each, 4)}'.replace(',', ' '))
        spent = client.cost_estimate(tokens, output, model)
        run_total = None if run_total is None or spent is None else run_total + spent
    # What is not in the variants is the pulses, built with the bot's model
    u = client.usage
    rest = client.cost_estimate(u['prompt'] - sum(t[0] for t in totals.values()),
                                u['output'] - sum(t[1] for t in totals.values()))
    run_total = None if run_total is None or rest is None else run_total + rest
    print(f'Весь прогон: {client.cost_text(run_total)}')
