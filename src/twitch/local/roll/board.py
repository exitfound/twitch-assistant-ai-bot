"""!rollboard, «Китежанин месяца»: who was a stream's достойнейший and подзалупный китежанин
most often this month.

A finished stream gives one title of each kind, by the rule the game itself uses
(storage.get_session_titles()), so extra throws do not inflate the count; the live stream
does not count until it is over, and the streamer, who throws without limit, is left out as
if they never threw. A stream where a single viewer played gives no titles: there was no rival.
The month is the one the stream started in, and a new month begins with its first stream. While
the month goes on, a tie on the first place shares it; once it is over, one of the tied wins by lot,
drawn once and kept, so the month always has one китежанин and one подзалупный. Below the first
place whoever reached the count first takes the place.
The first !roll of a month's first stream announces the previous month's results once, in
chat and on the stream.
"""
import asyncio
import dataclasses
import json
import logging
import random
from datetime import datetime

from src.core.config import Clock, Twitch
from src.core.content import Content
from src.core.database import get_last_stream, get_state, save_bot_interaction, set_state
from src.twitch.core.commands import PUBLIC_SCOPE, CommandContext
from src.twitch.core.limits import deny
from src.twitch.core.port import BotPort
from src.twitch.core.replies import reply
from src.twitch.local.roll import overlay
from src.twitch.local.roll.storage import get_month_sessions, get_session_titles
from src.twitch.local.roll.texts import MESSAGE_MAX, input_preview

logger = logging.getLogger(__name__)

TRIGGER = '!rollboard'
# Lines of a top: below the first place a tie does not add lines
TOP = 3
# Lines a tie on the first place shows at most: the rest is cut to «и ещё N»
ROWS = 5
# The last month whose results went out: a restart or a resumed stream must not announce them again
STATE_KEY = 'roll_board_month'
# Every !roll asks: once the month is announced, the answer stays in memory
_announced: str | None = None
# Two throws at once must not announce twice
_lock = asyncio.Lock()
# A finished month's winners, one key per month: the lot is drawn once, every later read shows the same
DRAW_KEY = 'roll_board_winners:{month}'
# Two first reads of a finished month must not draw twice
_draw_lock = asyncio.Lock()


@dataclasses.dataclass(frozen=True)
class Place:
    nick: str
    titles: int
    last: int       # index of the latest stream that gave the title: when the count was reached
    place: int = 0  # 1 for everyone tied on the first place, otherwise the line's number
    drawn: bool = False  # won a tied first place by lot


@dataclasses.dataclass(frozen=True)
class Top:
    places: tuple[Place, ...]
    more: int = 0   # tied on the first place but cut by ROWS


@dataclasses.dataclass(frozen=True)
class Board:
    month: str                  # YYYY-MM
    sessions: tuple[str, ...]   # the month's finished sessions with two viewers or more, oldest first
    champions: Top
    losers: Top


def month_of(session_id: str) -> str:
    return session_id[:7]


def previous_month(month: str) -> str:
    year, number = map(int, month.split('-'))
    return f'{year - 1}-12' if number == 1 else f'{year}-{number - 1:02d}'


def _count(table: dict[str, Place], title: tuple[str, int] | None, index: int) -> None:
    if title is None:
        return
    nick = title[0]
    old = table.get(nick)
    table[nick] = Place(nick, (old.titles if old else 0) + 1, index)


def _ranked(table: dict[str, Place]) -> list[Place]:
    """More titles first; equal counts go by who reached the count first."""
    return sorted(table.values(), key=lambda p: (-p.titles, p.last))


def _tied(table: dict[str, Place]) -> list[str]:
    """Everyone with the most titles, in ranked order."""
    ranked = _ranked(table)
    return [p.nick for p in ranked if p.titles == ranked[0].titles]


def _top(table: dict[str, Place], winner: str | None = None) -> Top:
    """TOP lines numbered by position, so a tie below the first place does not stretch the top.

    Without a winner everyone tied on the first place shares it and is shown (ROWS at most).
    A finished month's winner takes the first place alone, the rest follow by position.
    """
    ranked = _ranked(table)
    if not ranked:
        return Top(())
    if winner in table:
        tied = len(_tied(table)) > 1
        order = [table[winner]] + [p for p in ranked if p.nick != winner]
        return Top(tuple(dataclasses.replace(p, place=i, drawn=tied and i == 1)
                         for i, p in enumerate(order[:TOP], 1)))
    first = [dataclasses.replace(p, place=1) for p in ranked if p.titles == ranked[0].titles]
    rest = [dataclasses.replace(p, place=i) for i, p in enumerate(ranked[len(first):TOP], len(first) + 1)]
    return Top(tuple(first[:ROWS] + rest), max(0, len(first) - ROWS))


async def _tables(month: str, live: str | None) -> tuple[list[str], dict[str, Place], dict[str, Place]]:
    """The month's counted sessions and both title counts. live – the stream going on now: left out."""
    streamer = (Twitch.CHANNEL or '').lower() or None
    sessions: list[str] = []
    champions: dict[str, Place] = {}
    losers: dict[str, Place] = {}
    for session in await get_month_sessions(month):
        if session == live:
            continue
        champion, loser = await get_session_titles(session, without=streamer)
        if loser is None:
            # A single viewer: the title would come without a rival
            continue
        _count(champions, champion, len(sessions))
        _count(losers, loser, len(sessions))
        sessions.append(session)
    return sessions, champions, losers


async def standings(month: str, *, live: str | None = None) -> Board:
    """The month's board while it goes on: a tie on the first place is shared."""
    sessions, champions, losers = await _tables(month, live)
    return Board(month, tuple(sessions), _top(champions), _top(losers))


async def final(month: str) -> Board:
    """A finished month's board: one first place of each kind, a tie settled by lot.

    The lot is drawn on the first read after the month is over and kept in bot_state,
    so the announcement, !rollboard and the overlay name the same winner after a restart too.
    """
    async with _draw_lock:
        sessions, champions, losers = await _tables(month, None)
        if not sessions:
            return Board(month, (), Top(()), Top(()))
        saved = await get_state(DRAW_KEY.format(month=month))
        if saved is not None:
            winners = json.loads(saved)
        else:
            tied = {'champion': _tied(champions), 'loser': _tied(losers)}
            winners = {kind: random.choice(nicks) if nicks else None for kind, nicks in tied.items()}
            await set_state(DRAW_KEY.format(month=month), json.dumps(winners))
            for kind, nicks in tied.items():
                if len(nicks) > 1:
                    logger.info('Жребий за %s (%s): %s из %s', month, kind, winners[kind], ', '.join(nicks))
    return Board(month, tuple(sessions), _top(champions, winners.get('champion')),
                 _top(losers, winners.get('loser')))


async def current(session_id: str, live: bool) -> tuple[Board, bool]:
    """The board !rollboard shows and whether it is a finished month's.

    The month is the one the latest stream started in: a new month's board starts empty the
    moment its first stream goes live. Offline, session_id is today's date, so a month behind it is over.
    """
    if live:
        return await standings(month_of(session_id), live=session_id), False
    last = await get_last_stream()
    month = month_of(last.session_id if last else session_id)
    if month < month_of(session_id):
        return await final(month), True
    return await standings(month), False


def bounds(month: str) -> tuple[float, float]:
    """The month's start and the next one's in epoch seconds, by BOT_TIMEZONE: session ids use it."""
    year, number = map(int, month.split('-'))
    after = (year + 1, 1) if number == 12 else (year, number + 1)
    return (datetime(year, number, 1, tzinfo=Clock.ZONE).timestamp(),
            datetime(*after, 1, tzinfo=Clock.ZONE).timestamp())


def _first(top: Top) -> tuple[list[str], int]:
    """The nicks sharing the first place and how many of them ROWS cut."""
    return [p.nick for p in top.places if p.place == 1], top.more


async def regalia(session_id: str, live: bool) -> None:
    """The strip under the stream's titles: the last month's first places and this month's leaders.

    Called where the overlay reads the database (start, stream start and end): the leaders change
    only when a stream is over, and the month only when a stream of the next one starts. Never raises.
    """
    try:
        board, _final = await current(session_id, live)
        last = await final(previous_month(board.month))
        titles = board.champions.places[0].titles if board.champions.places else 0
        starts, ends = bounds(board.month)
        overlay.set_regalia(overlay.regalia(
            board.month, starts, ends, _first(board.champions), titles,
            last_month=last.month if last.sessions else None,
            last_champions=_first(last.champions), last_losers=_first(last.losers),
        ))
    except Exception:
        logger.exception('Регалии для оверлея не посчитаны')


def month_name(month: str) -> str:
    """«октябрь» from lists.months; the bare YYYY-MM if the list is not twelve lines."""
    names = Content.items('months')
    number = int(month[5:7])
    return names[number - 1] if len(names) == 12 else month


def _event(board: Board, final: bool) -> dict:
    def places(top: Top) -> list[tuple[str, int, int, bool]]:
        return [(p.nick, p.titles, p.place, p.drawn) for p in top.places]
    return overlay.board(board.month, final, len(board.sessions), places(board.champions), places(board.losers),
                         champions_more=board.champions.more, losers_more=board.losers.more)


def text(board: Board, *, final: bool) -> str:
    """One chat line: the month, its streams and both tops, the champions first.

    Long nicks in a long tie can outgrow what Twitch takes: the lines of each top are cut one
    by one into «и ещё N» until the line fits.
    """
    def places(top: Top, rows: int) -> str:
        if not top.places:
            return Content.text('rollboard_nobody')
        # The template holds one place; a comma goes between them
        line = ', '.join(Content.text('rollboard_place_drawn' if p.drawn else 'rollboard_place',
                                      place=p.place, nick=p.nick, titles=p.titles)
                         for p in top.places[:rows])
        more = top.more + max(0, len(top.places) - rows)
        return f"{line} {Content.text('rollboard_more', count=more)}" if more else line

    def render(rows: int) -> str:
        return Content.text('rollboard_final' if final else 'rollboard_month', month=month_name(board.month),
                            streams=len(board.sessions), champions=places(board.champions, rows),
                            losers=places(board.losers, rows))
    rows = ROWS
    line = render(rows)
    while len(line) > MESSAGE_MAX and rows > 1:
        rows -= 1
        line = render(rows)
    return line


async def handle_rollboard(ctx: CommandContext) -> None:
    if ctx.args:
        # «!rollboard 5»: the board takes nothing. The shared pause goes back, so a stray word does
        # not hold the board from the chat; the refusal is said once per 30 s, as there is no own cooldown
        ctx.bot.clear_cooldown(TRIGGER, PUBLIC_SCOPE)
        await deny(ctx.bot, ctx.message, ctx.user, Content.text(
            'rollboard_bad_input', user=ctx.user, input=input_preview(ctx.original_args),
        ))
        return
    board, final = await current(ctx.session_id, ctx.bot.stream_live)
    if not board.sessions:
        await reply(ctx.message, Content.text('rollboard_empty'))
        return
    if ctx.bot.stream_live:
        overlay.publish(_event(board, final))
    await reply(ctx.message, text(board, final=final))


async def announce(bot: BotPort, session_id: str) -> None:
    """After a !roll: on the month's first stream with the game, the previous month's results, once.
    Never raises.

    The month is marked done only once the line reached chat: a failure is retried by the
    next !roll. A stream of the month that had throws before means the announcement belongs
    to it, so nothing old is dug up.
    """
    global _announced
    previous = previous_month(month_of(session_id))
    if _announced == previous:
        return
    try:
        async with _lock:
            if await get_state(STATE_KEY) == previous:
                _announced = previous
                return
            if any(s != session_id for s in await get_month_sessions(month_of(session_id))):
                await set_state(STATE_KEY, previous)
                _announced = previous
                return
            board = await final(previous)
            if not board.sessions:
                await set_state(STATE_KEY, previous)
                _announced = previous
                return
            overlay.publish(_event(board, True))
            line = text(board, final=True)
            if not line or not await bot.send_chat_message(line):
                logger.warning('Итоги месяца %s не дошли до чата, повторю на следующем !roll', previous)
                return
            await set_state(STATE_KEY, previous)
            _announced = previous
        await save_bot_interaction(session_id, '_roll_', '[board]', line)
    except Exception:
        logger.exception('Итоги месяца не объявлены')
