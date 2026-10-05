"""Channel-points redemptions: what the bot says and what happens to the points."""
import asyncio

from fakes import FakeBot
from src.local.roll import game, redemption
from src.local.roll.redemption import handle_redemption
from src.local.roll.storage import save_roll


async def test_offline_redemption_is_refunded(db):
    bot = FakeBot(stream_live=False)
    assert await handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', '') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_offline')


async def test_successful_reward_is_fulfilled(db):
    bot = FakeBot()
    assert await handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', '') is True
    bot.send_chat_message.assert_awaited_once_with('texts.reward_shield_up')


async def test_refused_reward_is_refunded_with_its_reason(db):
    bot = FakeBot()
    assert await handle_redemption(bot, game.Action.REROLL, 'r1', 'gop', 'gop') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_self')


async def test_cleanse_of_yourself_has_its_own_text_and_no_roll_notes(db):
    bot = FakeBot()
    await save_roll(bot.session_id, 'gop', 50, free_throw=True)
    assert await handle_redemption(bot, game.Action.CURSE, 'r1', 'foe', 'gop') is True
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.CLEANSE, 'r2', 'gop', 'gop') is True
    bot.send_chat_message.assert_awaited_once_with('texts.reward_cleanse_self')
    assert await handle_redemption(bot, game.Action.CLEANSE, 'r3', 'gop', 'gop') is False
    bot.send_chat_message.assert_awaited_with('texts.reward_refund_not_cursed')


async def test_duplicate_is_left_alone_silently(db):
    bot = FakeBot()
    await handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', '')
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', '') is None
    bot.send_chat_message.assert_not_awaited()


async def test_first_reroll_of_a_player_uses_its_own_text(db):
    bot = FakeBot()
    await save_roll(bot.session_id, 'victim', 50, free_throw=True)
    await handle_redemption(bot, game.Action.REROLL, 'r1', 'gop', 'victim')
    assert bot.send_chat_message.await_args.args[0].startswith('texts.reward_reroll_done')


async def test_game_error_refunds(db, monkeypatch):
    async def broken(*args):
        raise RuntimeError('db gone')
    monkeypatch.setattr(game, 'redeem', broken)
    bot = FakeBot()
    assert await handle_redemption(bot, game.Action.EXTRA, 'r1', 'gop', '') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_error')


async def test_redelivered_after_the_stream_is_not_refunded_again(db):
    """EventSub may redeliver an applied redemption after the stream ended: chat must not
    hear a false «points refunded» for it."""
    bot = FakeBot()
    assert await handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', '') is True
    bot.stream_live = False
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', '') is None
    bot.send_chat_message.assert_not_awaited()


async def test_failing_save_still_settles_the_redemption(db, monkeypatch):
    """The reward is applied by then: an error while recording the message must not leave
    the redemption without a status until the next restart."""
    async def broken(*args):
        raise RuntimeError('db locked')
    monkeypatch.setattr(redemption, 'save_bot_interaction', broken)
    assert await handle_redemption(FakeBot(), game.Action.SHIELD, 'r1', 'gop', '') is True


async def test_an_extra_roll_after_a_burst_is_refunded(db):
    bot = FakeBot()
    for _ in range(3):
        await game.free_throw(bot.session_id, 'gop', limit=3)
    for i in range(2):
        assert await handle_redemption(bot, game.Action.EXTRA, f'r{i}', 'gop', '') is True
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.EXTRA, 'r9', 'gop', '') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_too_fast')


async def test_a_reroll_past_the_window_is_refunded_with_the_limit(db):
    bot = FakeBot()
    for i in range(4):
        await save_roll(bot.session_id, f't{i}', 50, free_throw=True)
    for i in range(3):
        assert await handle_redemption(bot, game.Action.REROLL, f'r{i}', 'gop', f't{i}') is True
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.REROLL, 'r9', 'gop', 't3') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_reroll_limit')


async def test_a_curse_past_the_stream_limit_is_refunded_with_the_limit(db):
    bot = FakeBot()
    for i in range(4):
        await save_roll(bot.session_id, f't{i}', 50, free_throw=True)
    for i in range(3):
        assert await handle_redemption(bot, game.Action.CURSE, f'r{i}', 'gop', f't{i}') is True
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.CURSE, 'r9', 'gop', 't3') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_curse_limit')


async def test_a_cleanse_past_the_stream_limit_is_refunded_with_the_limit(db, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(game.Rewards, 'LIMIT_FOLLOWER', 1)
    for i in range(2):
        await save_roll(bot.session_id, f't{i}', 50, free_throw=True)
        streamer = game.Twitch.CHANNEL.lower()     # curses without a limit of their own
        assert await handle_redemption(bot, game.Action.CURSE, f'c{i}', streamer, f't{i}') is True
    assert await handle_redemption(bot, game.Action.CLEANSE, 'r0', 'gop', 't0') is True
    bot.send_chat_message.reset_mock()
    assert await handle_redemption(bot, game.Action.CLEANSE, 'r1', 'gop', 't1') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_cleanse_limit')


async def test_one_redemption_delivered_twice_at_once_is_applied_once(db):
    """Both deliveries pass the first journal check before either is written; the game
    catches the second under its lock, and it must leave the status and the chat alone."""
    bot = FakeBot()
    results = await asyncio.gather(
        handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', ''),
        handle_redemption(bot, game.Action.SHIELD, 'r1', 'gop', ''),
    )
    assert sorted(results, key=str) == [None, True]
    bot.send_chat_message.assert_awaited_once_with('texts.reward_shield_up')
