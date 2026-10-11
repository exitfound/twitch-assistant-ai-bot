"""Channel-points redemptions: what the bot says and what happens to the points."""
import asyncio

import pytest

from fakes import FakeBot, make_chatter
from src.core.database import transaction
from src.twitch.local.roll import game, redemption, rules, tiers
from src.twitch.local.roll.redemption import handle_redemption
from src.twitch.local.roll.storage import count_actions, get_tier, save_roll, save_tier


def redeem(bot, action, redemption_id, user, user_input, *, follows=True, asked=None):
    """handle_redemption with Twitch's follow answer fixed; asked collects each question."""
    async def answer() -> bool:
        if asked is not None:
            asked.append(user)
        return follows
    return handle_redemption(bot, action, redemption_id, user, user_input, follows=answer)


async def test_offline_redemption_is_refunded(db):
    bot = FakeBot(stream_live=False)
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_offline')


async def test_successful_reward_is_fulfilled(db):
    bot = FakeBot()
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '') is True
    bot.send_chat_message.assert_awaited_once_with('texts.reward_shield_up')


async def test_refused_reward_is_refunded_with_its_reason(db):
    bot = FakeBot()
    assert await redeem(bot, game.Action.REROLL, 'r1', 'gop', 'gop') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_self')


async def test_cleanse_of_yourself_has_its_own_text_and_no_roll_notes(db, monkeypatch):
    """The curse threw above the old roll: nothing to return, the roll is unchanged."""
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: b)
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 50, free_throw=True)
    assert await redeem(bot, game.Action.CURSE, 'r1', 'foe', 'gop') is True
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.CLEANSE, 'r2', 'gop', 'gop') is True
    bot.send_chat_message.assert_awaited_once_with('texts.reward_cleanse_self')
    assert await redeem(bot, game.Action.CLEANSE, 'r3', 'gop', 'gop') is False
    bot.send_chat_message.assert_awaited_with('texts.reward_refund_not_cursed')


@pytest.mark.parametrize(('buyer', 'key'), [
    ('friend', 'texts.reward_cleanse_restored'),
    ('gop', 'texts.reward_cleanse_self_restored'),
])
async def test_a_cleanse_that_returns_the_roll_says_so_with_the_titles(db, monkeypatch, buyer, key):
    monkeypatch.setattr(rules.random, 'randint', lambda a, b: a)
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 90, free_throw=True)
    await save_roll(bot.session_id, 'other', 50, free_throw=True)
    assert await redeem(bot, game.Action.CURSE, 'r1', 'foe', 'gop') is True
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.CLEANSE, 'r2', buyer, 'gop') is True
    bot.send_chat_message.assert_awaited_once_with(f'{key} texts.roll_champion')


async def test_duplicate_is_left_alone_silently(db):
    bot = FakeBot()
    await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '')
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '') is None
    bot.send_chat_message.assert_not_awaited()


async def test_first_reroll_of_a_player_uses_its_own_text(db):
    bot = FakeBot()
    await save_roll(bot.session_id, 'victim', 50, free_throw=True)
    await redeem(bot, game.Action.REROLL, 'r1', 'gop', 'victim')
    assert bot.send_chat_message.await_args.args[0].startswith('texts.reward_reroll_done')


async def test_game_error_refunds(db, monkeypatch):
    async def broken(*args):
        raise RuntimeError('db gone')
    monkeypatch.setattr(game, 'redeem', broken)
    bot = FakeBot()
    assert await redeem(bot, game.Action.EXTRA, 'r1', 'gop', '') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_error')


async def test_redelivered_after_the_stream_is_not_refunded_again(db):
    """EventSub may redeliver an applied redemption after the stream ended: chat must not
    hear a false «points refunded» for it."""
    bot = FakeBot()
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '') is True
    bot.stream_live = False
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '') is None
    bot.send_chat_message.assert_not_awaited()


async def test_failing_save_still_settles_the_redemption(db, monkeypatch):
    """The reward is applied by then: an error while recording the message must not leave
    the redemption without a status until the next restart."""
    async def broken(*args):
        raise RuntimeError('db locked')
    monkeypatch.setattr(redemption, 'save_bot_interaction', broken)
    assert await redeem(FakeBot(), game.Action.SHIELD, 'r1', 'gop', '') is True


async def test_an_extra_roll_after_a_burst_is_refunded(db):
    bot = FakeBot()
    for _ in range(3):
        await game.free_throw(bot.session_id, 'gop', limit=3)
    for i in range(2):
        assert await redeem(bot, game.Action.EXTRA, f'r{i}', 'gop', '') is True
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.EXTRA, 'r9', 'gop', '') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_too_fast')


async def test_a_reroll_past_the_window_is_refunded_with_the_limit(db):
    bot = FakeBot()
    for i in range(4):
        await save_roll(bot.session_id, f't{i}', 50, free_throw=True)
    for i in range(3):
        assert await redeem(bot, game.Action.REROLL, f'r{i}', 'gop', f't{i}') is True
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.REROLL, 'r9', 'gop', 't3') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_reroll_limit texts.sub_hint')


async def test_a_curse_past_the_stream_limit_is_refunded_with_the_limit(db):
    bot = FakeBot()
    for i in range(4):
        await save_roll(bot.session_id, f't{i}', 50, free_throw=True)
    for i in range(3):
        assert await redeem(bot, game.Action.CURSE, f'r{i}', 'gop', f't{i}') is True
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.CURSE, 'r9', 'gop', 't3') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_curse_limit texts.sub_hint')


async def test_a_cleanse_past_the_stream_limit_is_refunded_with_the_limit(db, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(game.Rewards, 'LIMIT_FOLLOWER', 1)
    for i in range(2):
        await save_roll(bot.session_id, f't{i}', 50, free_throw=True)
        streamer = game.Twitch.CHANNEL.lower()     # curses without a limit of their own
        assert await redeem(bot, game.Action.CURSE, f'c{i}', streamer, f't{i}') is True
    assert await redeem(bot, game.Action.CLEANSE, 'r0', 'gop', 't0') is True
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.CLEANSE, 'r1', 'gop', 't1') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_cleanse_limit texts.sub_hint')


async def test_one_redemption_delivered_twice_at_once_is_applied_once(db):
    """Both deliveries pass the first journal check before either is written; the game
    catches the second under its lock, and it must leave the status and the chat alone."""
    bot = FakeBot()
    results = await asyncio.gather(
        redeem(bot, game.Action.SHIELD, 'r1', 'gop', ''),
        redeem(bot, game.Action.SHIELD, 'r1', 'gop', ''),
    )
    assert sorted(results, key=str) == [None, True]
    bot.send_chat_message.assert_awaited_once_with('texts.reward_shield_up')


async def test_a_non_follower_gets_the_points_back(db):
    """Points accrue without a follow, but the game is closed to a non-follower, as in chat."""
    bot = FakeBot()
    await save_roll(bot.session_id, 'victim', 50, free_throw=True)
    assert await redeem(bot, game.Action.CURSE, 'r1', 'gop', 'victim', follows=False) is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_follow')
    assert await count_actions(bot.session_id, 'gop', game.Action.CURSE) == 0


@pytest.mark.parametrize('live, text', [(True, 'texts.reward_refund_follow'), (False, 'texts.reward_refund_offline')])
async def test_a_redelivered_refusal_is_skipped(db, live, text):
    """EventSub may send a redemption twice: chat hears the refusal once, and Twitch gets
    no second status for a redemption already cancelled."""
    bot = FakeBot(stream_live=live)
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '', follows=False) is False
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '', follows=False) is None
    bot.send_chat_message.assert_awaited_once_with(text)


async def test_a_badge_seen_in_chat_skips_the_follow_question(db):
    """A VIP, subscriber or moderator plays without a follow, as the chat gate lets them."""
    bot = FakeBot()
    asked = []
    for user, tier in (('vip', 'vip'), ('sub', 'subscriber'), ('mod', 'moderator')):
        await save_tier(bot.session_id, user, tier)
        assert await redeem(bot, game.Action.SHIELD, f'r-{user}', user, '', follows=False, asked=asked) is True
    assert asked == []


async def test_a_follower_seen_in_chat_is_still_asked(db):
    """A regular status says nothing about a follow: Twitch is asked."""
    bot = FakeBot()
    await save_tier(bot.session_id, 'gop', 'regular')
    asked = []
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '', asked=asked) is True
    assert asked == ['gop']


async def test_the_broadcaster_and_an_open_channel_skip_the_follow_question(db, monkeypatch):
    bot = FakeBot()
    asked = []
    streamer = game.Twitch.CHANNEL.lower()
    assert await redeem(bot, game.Action.SHIELD, 'r1', streamer, '', follows=False, asked=asked) is True
    monkeypatch.setattr(redemption.Follow, 'REQUIRED', False)
    assert await redeem(bot, game.Action.SHIELD, 'r2', 'gop', '', follows=False, asked=asked) is True
    assert asked == []


async def test_chat_stores_the_status_once_and_again_on_a_new_badge(db, monkeypatch):
    """The rewards see no badges: chat writes them, but not on every message."""
    writes = []
    real = tiers.save_tier

    async def counted(*args):
        writes.append(args)
        await real(*args)
    monkeypatch.setattr(tiers, 'save_tier', counted)
    bot = FakeBot()
    for chatter in (make_chatter('gop', vip=True), make_chatter('gop', vip=True), make_chatter('gop', subscriber=True)):
        await tiers.on_chat(bot, bot.session_id, chatter)
    assert [w[2] for w in writes] == ['vip', 'subscriber']
    assert await get_tier(bot.session_id, 'gop') == 'subscriber'


async def test_offline_chat_stores_no_status(db):
    bot = FakeBot(stream_live=False)
    await tiers.on_chat(bot, bot.session_id, make_chatter('gop', vip=True))
    assert await get_tier(bot.session_id, 'gop') is None


async def test_a_subscriber_seen_in_chat_buys_by_their_status_before_any_roll(db):
    """The status comes with the first message, not with the first !roll."""
    bot = FakeBot()
    await tiers.on_chat(bot, bot.session_id, make_chatter('gop', subscriber=True))
    for i in range(game.Rewards.LIMIT_FOLLOWER + 1):
        assert await redeem(bot, game.Action.SHIELD, f'r{i}', 'gop', '', follows=False) is True
        async with transaction() as db_:
            # The shield expires; the per-stream count keeps every one of them
            await db_.execute("UPDATE roll_actions SET created_at = datetime('now', '-1 day')")


@pytest.mark.parametrize(('tier', 'sub_limit', 'hint'), [
    (None, 2, True), ('regular', 2, True), ('vip', 2, True), ('moderator', 2, True),
    ('moderator', 1, False), ('subscriber', 1, False),
])
async def test_a_limit_refusal_hints_at_a_subscription_where_it_would_help(db, monkeypatch, tier,
                                                                            sub_limit, hint):
    """A subscription raises the buyer's limit, so the refusal says so – as in chat; not
    where the subscriber's limit is no higher."""
    monkeypatch.setattr(game.Rewards, 'LIMIT_FOLLOWER', 1)
    monkeypatch.setattr(game.Rewards, 'LIMIT_VIP', 1)
    monkeypatch.setattr(game.Rewards, 'LIMIT_MOD', 1)
    monkeypatch.setattr(game.Rewards, 'LIMIT_SUB', sub_limit)
    bot = FakeBot()
    if tier is not None:
        await save_tier(bot.session_id, 'gop', tier)
    assert await redeem(bot, game.Action.SHIELD, 'r1', 'gop', '') is True
    async with transaction() as db_:
        await db_.execute("UPDATE roll_actions SET created_at = datetime('now', '-1 day')")
    bot.send_chat_message.reset_mock()
    assert await redeem(bot, game.Action.SHIELD, 'r2', 'gop', '') is False
    expected = 'texts.reward_refund_shield_limit' + (' texts.sub_hint' if hint else '')
    bot.send_chat_message.assert_awaited_once_with(expected)
