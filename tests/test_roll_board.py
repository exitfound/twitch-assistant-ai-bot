"""!rollboard, «Китежанин месяца»: the month's top threes of the finished streams' titles."""
import asyncio
import json

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core.config import Roll, Twitch
from src.core.database import get_state, save_stream
from src.core.utils import local_time
from src.twitch.core import sse
from src.twitch.core.commands import PUBLIC_SCOPE, CommandContext
from src.twitch.core.component import ChatComponent
from src.twitch.local.roll import board, overlay
from src.twitch.local.roll.command import handle_roll
from src.twitch.local.roll.storage import save_roll


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(Roll, 'OVERLAY_ENABLED', True)
    queue = asyncio.Queue(sse.QUEUE_SIZE)
    overlay.feed.clients.add(queue)
    return queue


def sent(queue: asyncio.Queue) -> list[dict]:
    return [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]


async def stream(session_id: str, rolls: dict[str, int]) -> None:
    for user, value in rolls.items():
        await save_roll(session_id, user, value, free_throw=True)


def names(top: board.Top) -> list[tuple[str, int, int]]:
    """(nick, titles, place) of each line."""
    return [(p.nick, p.titles, p.place) for p in top.places]


async def call(bot: FakeBot, user: str = 'gop', args: str = ''):
    text = f'!rollboard {args}'.strip()
    message = make_message(text, make_chatter(user))
    await board.handle_rollboard(CommandContext(message=message, user=user, prompt=text, original_text=text,
                                                session_id=bot.session_id, bot=bot, args=args))
    return message


async def test_a_stream_gives_one_title_of_each_kind_in_the_month_it_started(db):
    # Past midnight on the 30th the stream still belongs to September
    await stream('2026-09-30 22:00', {'sept': 90, 'other': 5})
    await stream('2026-10-01 20:00', {'gop': 3, 'champ': 99, 'mid': 50})
    await stream('2026-10-03 20:00', {'gop': 7, 'champ': 98, 'mid': 60})
    await stream('2026-10-05', {'mid': 1, 'gop': 80})
    result = await board.standings('2026-10')
    assert result.sessions == ('2026-10-01 20:00', '2026-10-03 20:00', '2026-10-05')
    assert names(result.champions) == [('champ', 2, 1), ('gop', 1, 2)]
    assert names(result.losers) == [('gop', 2, 1), ('mid', 1, 2)]


async def test_a_tie_on_the_first_place_shares_it_ordered_by_who_reached_the_count_first(db):
    await stream('2026-10-01 20:00', {'low': 2, 'top': 100, 'x': 50})
    await stream('2026-10-02 20:00', {'early': 9, 'high': 97, 'x': 50})
    await stream('2026-10-03 20:00', {'late': 1, 'later': 99, 'x': 50})
    result = await board.standings('2026-10')
    assert names(result.losers) == [('low', 1, 1), ('early', 1, 1), ('late', 1, 1)]
    assert names(result.champions) == [('top', 1, 1), ('high', 1, 1), ('later', 1, 1)]


async def test_a_shared_first_place_takes_up_the_places_after_it(db):
    """Two on the first place: the next is third, and only one line is left for it."""
    for day, champ in enumerate(['a', 'b', 'b', 'a', 'c', 'd'], 1):
        await stream(f'2026-10-{day:02d} 20:00', {champ: 99, 'x': 1})
    assert names((await board.standings('2026-10')).champions) == [('b', 2, 1), ('a', 2, 1), ('c', 1, 3)]


async def test_a_tie_below_the_first_place_gives_no_extra_lines(db):
    """Three lines in all: below the first place whoever reached the count first takes it."""
    for day, champ in enumerate(['a', 'a', 'a', 'b', 'c', 'c', 'b', 'd', 'd', 'e'], 1):
        await stream(f'2026-10-{day:02d} 20:00', {champ: 99, 'x': 1})
    result = await board.standings('2026-10')
    assert names(result.champions) == [('a', 3, 1), ('c', 2, 2), ('b', 2, 3)]
    assert result.champions.more == 0


async def test_a_long_tie_is_cut_to_five_lines_and_a_count(db):
    for day in range(1, 8):
        await stream(f'2026-10-{day:02d} 20:00', {f'p{day}': 99, 'x': 1})
    result = await board.standings('2026-10')
    assert len(result.champions.places) == board.ROWS and result.champions.more == 2
    assert all(p.place == 1 for p in result.champions.places)
    assert board.text(result, final=False) == 'texts.rollboard_month'


async def test_a_line_too_long_for_twitch_folds_the_tie_into_a_count(db, monkeypatch):
    """Long nicks in a long tie: lines go into «и ещё N» until Twitch takes the message."""
    for day in range(1, 8):
        await stream(f'2026-10-{day:02d} 20:00',
                     {f'champion_number_{day:02d}_xx': 99, f'loser_number_{day:02d}_xxxxx': 1})
    texts = {'rollboard_place': '{place}. {nick} ×{titles}', 'rollboard_more': 'и ещё {count}',
             'rollboard_month': '{champions} | {losers} ' + 'х' * 200}
    monkeypatch.setattr(board.Content, 'text', lambda key, **kw: texts[key].format(**kw))
    line = board.text(await board.standings('2026-10'), final=False)
    assert len(line) <= board.MESSAGE_MAX
    assert line.count('champion_number') == 4 and 'и ещё 3 |' in line


async def test_the_streamer_is_left_out_and_their_titles_go_to_the_next(db, monkeypatch):
    """The streamer throws without limit: on the board the stream is judged as if they never threw."""
    monkeypatch.setattr(Twitch, 'CHANNEL', 'Streamer')
    await stream('2026-10-01 20:00', {'streamer': 100, 'a': 50, 'b': 10})
    await stream('2026-10-02 20:00', {'streamer': 1, 'a': 50, 'b': 60})
    # With the streamer left out one viewer stays: the stream gives nothing
    await stream('2026-10-03 20:00', {'streamer': 5, 'alone': 40})
    result = await board.standings('2026-10')
    assert len(result.sessions) == 2
    assert names(result.champions) == [('a', 1, 1), ('b', 1, 1)]
    assert names(result.losers) == [('b', 1, 1), ('a', 1, 1)]


async def test_a_stream_with_a_single_viewer_gives_no_titles(db):
    """In the game a lone player is the champion; on the board that title would come without a rival."""
    await stream('2026-10-01 20:00', {'alone': 40})
    await stream('2026-10-02 20:00', {'gop': 3, 'champ': 99})
    result = await board.standings('2026-10')
    assert result.sessions == ('2026-10-02 20:00',)
    assert names(result.champions) == [('champ', 1, 1)]


async def test_in_a_stream_the_finished_ones_go_to_chat_and_to_the_page(db, page):
    await stream('2026-10-01 20:00', {'gop': 3, 'champ': 99})
    # The stream going on counts only once it is over: its titles are not final yet
    await stream('2026-10-03 20:00', {'now_low': 1, 'now_high': 100})
    bot = FakeBot(session_id='2026-10-03 20:00')
    message = await call(bot)
    message.respond.assert_awaited_once_with('texts.rollboard_month')
    [event] = sent(page)
    assert event == {'type': 'board', 'month': '2026-10', 'final': False, 'streams': 1,
                     'champions': [{'nick': 'champ', 'titles': 1, 'place': 1, 'drawn': False}],
                     'losers': [{'nick': 'gop', 'titles': 1, 'place': 1, 'drawn': False}],
                     'more': {'champions': 0, 'losers': 0}}


async def test_the_months_first_stream_starts_its_board_empty(db, page):
    """The month turns over the moment its first stream goes live: the last month is not shown."""
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    await stream('2026-10-01 20:00', {'now_low': 1, 'now_high': 100})
    message = await call(FakeBot(session_id='2026-10-01 20:00'))
    message.respond.assert_awaited_once_with('texts.rollboard_empty')
    assert sent(page) == []


async def test_a_new_months_stream_without_throws_still_turns_the_month(db):
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    await save_stream('s1', '2026-09-20 20:00', 1)
    await save_stream('s2', '2026-10-01 20:00', 2)
    message = await call(FakeBot(session_id='2026-10-02', stream_live=False))
    message.respond.assert_awaited_once_with('texts.rollboard_empty')


async def test_offline_before_the_months_first_stream_the_last_month_stands_as_final(db, page):
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    await save_stream('s1', '2026-09-20 20:00', 1)
    bot = FakeBot(session_id='2026-10-01', stream_live=False)
    message = await call(bot)
    message.respond.assert_awaited_once_with('texts.rollboard_final')
    assert sent(page) == []


async def test_offline_in_a_month_with_streams_the_board_is_this_months(db):
    await stream('2026-10-01 20:00', {'gop': 3, 'champ': 99})
    await save_stream('s1', '2026-10-01 20:00', 1)
    message = await call(FakeBot(session_id='2026-10-02', stream_live=False))
    message.respond.assert_awaited_once_with('texts.rollboard_month')


async def test_a_word_after_the_command_is_refused_and_does_not_hold_the_board(db, page):
    """No own cooldown: the refusal is said once per 30 s, and the shared pause goes back."""
    await stream('2026-10-01 20:00', {'gop': 3, 'champ': 99})
    bot = FakeBot(session_id='2026-10-03 20:00')
    bot.set_cooldown(board.TRIGGER, 30, PUBLIC_SCOPE)
    first = await call(bot, args='5')
    first.respond.assert_awaited_once_with('texts.rollboard_bad_input')
    assert not bot.cooldown_remaining(board.TRIGGER, PUBLIC_SCOPE)
    again = await call(bot, args='топ')
    again.respond.assert_not_awaited()
    assert sent(page) == []


def test_the_month_names_come_from_the_content(monkeypatch):
    monkeypatch.setattr(board.Content, 'items', lambda name: [f'm{i}' for i in range(1, 13)])
    assert board.month_name('2026-10') == 'm10'
    monkeypatch.setattr(board.Content, 'items', lambda name: [])
    assert board.month_name('2026-10') == '2026-10'


def test_the_previous_month_crosses_the_year():
    assert board.previous_month('2026-01') == '2025-12'
    assert board.previous_month('2026-10') == '2026-09'


async def test_the_months_first_stream_announces_the_last_month_once(db, page):
    """Said once: a restart or a resumed stream says nothing again."""
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    bot = FakeBot(session_id='2026-10-01 20:00')
    await board.announce(bot, bot.session_id)
    bot.send_chat_message.assert_awaited_once_with('texts.rollboard_final')
    [event] = sent(page)
    assert event['final'] and event['month'] == '2026-09'
    await board.announce(bot, bot.session_id)
    # A restart forgets the memory, the database still knows
    board._announced = None
    await board.announce(bot, bot.session_id)
    bot.send_chat_message.assert_awaited_once()
    assert await get_state(board.STATE_KEY) == '2026-09'


async def test_results_that_did_not_reach_chat_are_tried_again_on_the_next_roll(db, page):
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    bot = FakeBot(session_id='2026-10-01 20:00')
    bot.send_chat_message.return_value = False
    await board.announce(bot, bot.session_id)
    assert await get_state(board.STATE_KEY) is None
    # The stream got more throws meanwhile: it is still the month's first
    await stream(bot.session_id, {'gop': 50})
    bot.send_chat_message.return_value = True
    await board.announce(bot, bot.session_id)
    assert bot.send_chat_message.await_count == 2
    assert await get_state(board.STATE_KEY) == '2026-09'


async def test_a_later_stream_of_the_month_digs_up_nothing(db, page):
    """The first start after the feature ships mid-month must not announce an old month."""
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    await stream('2026-10-01 20:00', {'gop': 3, 'champ': 99})
    bot = FakeBot(session_id='2026-10-03 20:00')
    await board.announce(bot, bot.session_id)
    bot.send_chat_message.assert_not_awaited()
    assert sent(page) == []


async def test_after_an_empty_month_the_first_stream_announces_nothing(db):
    bot = FakeBot(session_id='2026-10-01 20:00')
    await board.announce(bot, bot.session_id)
    bot.send_chat_message.assert_not_awaited()


async def test_the_first_roll_of_the_stream_announces_the_last_month(db, page):
    """The results go to a chat that is playing, not to the empty one at the stream's start."""
    await stream('2026-09-20 20:00', {'gop': 3, 'champ': 99})
    bot = FakeBot(session_id='2026-10-01 20:00')
    for user in ('first', 'second'):
        message = make_message('!roll', make_chatter(user))
        await handle_roll(CommandContext(message=message, user=user, prompt='!roll', original_text='!roll',
                                         session_id=bot.session_id, bot=bot))
    bot.send_chat_message.assert_awaited_once_with('texts.rollboard_final')
    assert [e['type'] for e in sent(page)] == ['roll', 'board', 'roll']


def test_the_board_has_one_shared_pause_like_help():
    entry = ChatComponent(FakeBot())._registry.resolve('!rollboard')
    assert entry.handler is board.handle_rollboard and entry.public


async def test_regalia_carry_the_last_months_first_places_and_this_months_leader(db, page):
    await stream('2026-09-10 20:00', {'gop': 3, 'champ': 99, 'mid': 50})
    await stream('2026-09-12 20:00', {'mid': 2, 'other': 98, 'gop': 50})
    await stream('2026-10-01 20:00', {'low': 4, 'lead': 90})
    await stream('2026-10-03 20:00', {'now_low': 1, 'now_high': 100})
    await board.regalia('2026-10-03 20:00', live=True)
    [event] = sent(page)
    assert event['type'] == 'regalia' and event['month'] == '2026-10'
    # The live stream does not count: the leader is the finished stream's
    assert event['leaders'] == ['lead'] and event['titles'] == 1 and event['more'] == 0
    # September ended in a tie on both first places: the lot gives each to one of the tied
    assert event['last']['month'] == '2026-09'
    assert len(event['last']['champions']) == 1 and event['last']['champions'][0] in ('champ', 'other')
    assert len(event['last']['losers']) == 1 and event['last']['losers'][0] in ('gop', 'mid')
    starts, ends = board.bounds('2026-10')
    assert (event['starts'], event['ends']) == (starts, ends)
    assert overlay._snapshot()['regalia'] == event


async def test_regalia_on_the_months_first_stream_have_no_leader_yet(db, page):
    await stream('2026-09-10 20:00', {'gop': 3, 'champ': 99})
    await stream('2026-10-01 20:00', {'now_low': 1, 'now_high': 100})
    await board.regalia('2026-10-01 20:00', live=True)
    [event] = sent(page)
    assert event['leaders'] == [] and event['titles'] == 0
    assert event['last']['champions'] == ['champ']


async def test_regalia_without_a_last_month_leave_it_out(db, page):
    await board.regalia('2026-10-01 20:00', live=True)
    [event] = sent(page)
    assert event['last'] is None and event['month'] == '2026-10'


async def test_regalia_offline_follow_the_last_stream_started(db, page):
    """Offline the strip belongs to the month of the latest stream, like !rollboard."""
    await stream('2026-09-10 20:00', {'gop': 3, 'champ': 99})
    await save_stream('s1', '2026-09-10 20:00', 1)
    await board.regalia('2026-10-02', live=False)
    [event] = sent(page)
    assert event['month'] == '2026-09' and event['leaders'] == ['champ']


def test_month_bounds_follow_the_bots_zone():
    starts, ends = board.bounds('2026-12')
    assert local_time(starts).strftime('%Y-%m-%d %H:%M') == '2026-12-01 00:00'
    assert local_time(ends).strftime('%Y-%m-%d %H:%M') == '2027-01-01 00:00'


async def test_a_finished_months_tie_is_drawn_once_and_kept(db, monkeypatch):
    await stream('2026-09-10 20:00', {'gop': 3, 'a': 99, 'mid': 50})
    await stream('2026-09-12 20:00', {'mid': 2, 'b': 98, 'gop': 50})
    await stream('2026-09-14 20:00', {'low': 1, 'c': 97})
    picks = iter(['b', 'mid'])
    monkeypatch.setattr(board.random, 'choice', lambda nicks: next(picks))
    result = await board.final('2026-09')
    # The winner takes the first place alone, the rest of the tie follows by position
    assert names(result.champions) == [('b', 1, 1), ('a', 1, 2), ('c', 1, 3)]
    assert [p.drawn for p in result.champions.places] == [True, False, False]
    assert names(result.losers)[0] == ('mid', 1, 1)
    assert json.loads(await get_state('roll_board_winners:2026-09')) == {'champion': 'b', 'loser': 'mid'}
    # A second read draws nothing: the kept winners stand
    monkeypatch.setattr(board.random, 'choice', lambda nicks: pytest.fail('drawn twice'))
    again = await board.final('2026-09')
    assert again.champions == result.champions and again.losers == result.losers


async def test_a_clear_first_place_is_no_lot(db):
    await stream('2026-09-10 20:00', {'gop': 3, 'a': 99})
    await stream('2026-09-12 20:00', {'gop': 2, 'a': 98, 'b': 50})
    result = await board.final('2026-09')
    assert names(result.champions) == [('a', 2, 1)] and not result.champions.places[0].drawn
    assert board.text(result, final=True) == 'texts.rollboard_final'


async def test_the_drawn_place_has_its_own_text(db, monkeypatch):
    await stream('2026-09-10 20:00', {'gop': 3, 'a': 99})
    await stream('2026-09-12 20:00', {'mid': 2, 'b': 98})
    monkeypatch.setattr(board.random, 'choice', lambda nicks: nicks[-1])
    result = await board.final('2026-09')
    monkeypatch.setattr(board.Content, 'text', lambda key, **kw: f'{key}:{kw.get("nick", "")}' if 'place' in key
                        else f"{kw.get('champions', '')}|{kw.get('losers', '')}")
    assert board.text(result, final=True) == ('rollboard_place_drawn:b, rollboard_place:a|'
                                              'rollboard_place_drawn:mid, rollboard_place:gop')
