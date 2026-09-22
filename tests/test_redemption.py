"""Channel-points redemptions: what the bot says and what happens to the points."""
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
    bot.send_chat_message.assert_awaited_once_with('texts.reward_shield_done')


async def test_refused_reward_is_refunded_with_its_reason(db):
    bot = FakeBot()
    assert await handle_redemption(bot, game.Action.REROLL, 'r1', 'gop', 'gop') is False
    bot.send_chat_message.assert_awaited_once_with('texts.reward_refund_self')


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
