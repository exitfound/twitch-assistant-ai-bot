"""Command matching and routing: which message reaches which handler, and on what text."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.twitch.core.commands import CommandContext, CommandEntry, Kind
from src.twitch.core.component import ChatComponent, route
from src.core.config import Follow, Quota
from src.core.database import count_bot_uses, record_bot_use
from src.twitch.gemini.commands import handle_who
from src.twitch.local.channel import handle_help_channel
from src.twitch.local.commands import handle_help, handle_help_index, handle_stats
from src.twitch.local.roll.command import handle_roll, handle_rollstat


async def _noop(ctx):
    pass


class TestCommandEntry:
    def test_exact_entry_matches_only_itself(self):
        entry = CommandEntry('!roll', _noop, prefix=False, kind='local')
        assert entry.match('!roll')
        assert not entry.match('!roll 5')
        assert entry.extract_args('!roll') == ''

    def test_prefix_entry_needs_a_word_boundary(self):
        entry = CommandEntry('!who', _noop, prefix=True, kind='local')
        assert entry.match('!who')
        assert entry.match('!who nick')
        assert not entry.match('!whoever')

    @pytest.mark.parametrize('prompt', ['!ask: вопрос', '!ask:вопрос', '!ask, вопрос', '!ask вопрос'])
    def test_separators_before_args(self, prompt):
        entry = CommandEntry('!ask', _noop, prefix=True, kind='local')
        assert entry.match(prompt)
        assert entry.extract_args(prompt) == 'вопрос'


@pytest.fixture
def component():
    return ChatComponent(FakeBot())


@pytest.mark.parametrize('text', ['!ask вопрос', '!who nick', '!versus a b', '!summary'])
async def test_per_stream_commands_are_open_to_a_follower(db, monkeypatch, text):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    component = ChatComponent(FakeBot())
    handler = AsyncMock()
    monkeypatch.setattr(component._registry.resolve(text), 'handler', handler)
    message = make_message(text)
    await component.event_message(message)
    handler.assert_awaited_once()


# --- follow gate ---------------------------------------------------------------

@pytest.fixture
def gated(monkeypatch):
    """A component with the follow requirement on and !stat's handler replaced by a mock."""
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    bot = FakeBot()
    component = ChatComponent(bot)
    handler = AsyncMock()
    monkeypatch.setattr(component._registry.resolve('!stat'), 'handler', handler)
    return component, bot, handler


async def test_a_viewer_who_does_not_follow_is_refused_once(db, gated):
    """The hint repeats at most once per FOLLOW_HINT_MINUTES: command spam must not turn
    into refusal spam."""
    component, bot, handler = gated
    bot.follows = False
    first, second = make_message('!stat', make_chatter('gop')), make_message('!stat', make_chatter('gop'))
    await component.event_message(first)
    await component.event_message(second)
    handler.assert_not_awaited()
    first.respond.assert_awaited_once_with('texts.follow_required')
    second.respond.assert_not_awaited()


async def test_a_follower_passes_and_is_cached(db, gated):
    component, bot, handler = gated
    await component.event_message(make_message('!stat', make_chatter('gop')))
    handler.assert_awaited_once()
    # The check comes before the cooldown, so a repeat within it still asks the cache
    await component.event_message(make_message('!stat', make_chatter('gop')))
    assert bot.follow_checks == 1


async def test_a_new_follow_is_not_held_back_by_the_cache(db, gated):
    """A viewer refused a minute ago follows: event_follow drops the cached «no»."""
    component, bot, handler = gated
    bot.follows = False
    await component.event_message(make_message('!stat', make_chatter('gop')))
    bot.follows = True
    component._followers.forget('id-gop')
    await component.event_message(make_message('!stat', make_chatter('gop')))
    handler.assert_awaited_once()


async def test_a_helix_error_lets_the_viewer_in(db, gated):
    """A silent Twitch must not lock the chat out of the bot."""
    component, bot, handler = gated
    bot.follows = RuntimeError('helix down')
    await component.event_message(make_message('!stat', make_chatter('gop')))
    handler.assert_awaited_once()


async def test_badges_and_help_skip_the_follow_check(db, gated, monkeypatch):
    component, bot, handler = gated
    bot.follows = False
    await component.event_message(make_message('!stat', make_chatter('vip', vip=True)))
    handler.assert_awaited_once()
    help_handler = AsyncMock()
    monkeypatch.setattr(component._registry.resolve('!bot'), 'handler', help_handler)
    await component.event_message(make_message('!bot', make_chatter('gop')))
    help_handler.assert_awaited_once()
    assert bot.follow_checks == 0


@pytest.mark.parametrize(('text', 'handler'), [
    ('!help', handle_help_index),
    ('!bot', handle_help),
    ('!channel', handle_help_channel),
    ('!stat', handle_stats),
    ('!stat nick', handle_stats),
    ('!rollstat', handle_rollstat),
    ('!roll', handle_roll),
    ('!roll 5', handle_roll),
    ('!roll: @nick', handle_roll),
    ('!rollstat 5', None),
    ('!who nick', handle_who),
])
def test_registry_resolves_the_real_commands(component, text, handler):
    entry = component._registry.resolve(text)
    assert (entry.handler if entry else None) is handler


@pytest.mark.parametrize('text', ['!whoever', '!rolls', '!rollstats', 'привет'])
def test_registry_ignores_near_misses(component, text):
    assert component._registry.resolve(text) is None


class TestRoute:
    def test_bare_command_needs_no_addressing(self, component):
        entry, prompt, addressed = component._route(make_message('!who Nick'))
        assert entry.handler is handle_who
        assert prompt == '!who nick'
        assert addressed is False

    def test_plain_chat_is_not_for_the_bot(self, component):
        assert component._route(make_message('просто болтаем')) == (None, None, False)

    @pytest.mark.parametrize('text', ['сосурян, как дела', 'SECURITYEXPERT как дела', '@sosuryan_bot как дела'])
    def test_free_text_addressed_by_word_or_mention(self, component, text):
        entry, prompt, addressed = component._route(make_message(text))
        assert entry is None
        assert addressed is True
        assert 'как дела' in prompt

    def test_reply_to_the_bot_is_addressing(self, component):
        reply = SimpleNamespace(parent_user=SimpleNamespace(id='1000'), parent_message_body='реплика')
        entry, prompt, addressed = component._route(make_message('а почему', reply=reply))
        assert (entry, prompt, addressed) == (None, 'а почему', True)

    @pytest.mark.parametrize('text', [
        'сосурян, кто такой securityexpert?',
        '@sosuryan_bot кто такой securityexpert?',
    ])
    def test_only_the_addressing_is_cut_from_the_question(self, component, text):
        """A nick like securityexpert in the question itself must reach the model."""
        _, prompt, addressed = component._route(make_message(text))
        assert addressed
        assert 'securityexpert' in prompt
        assert 'sosuryan_bot' not in prompt

    def test_a_longer_nick_is_not_the_bot(self, component):
        assert component._route(make_message('@sosuryan_bot_fan привет')) == (None, None, False)

    def test_free_text_keeps_its_case(self, component):
        """«РФ», «IT» and names lose their meaning in lowercase."""
        entry, prompt, _ = component._route(make_message('Сосурян, что думаешь про IT в РФ, Nosok222?'))
        assert entry is None
        assert 'что думаешь про IT в РФ, Nosok222?' in prompt

    def test_a_command_after_the_call_word_is_found_in_any_case(self, component):
        entry, prompt, _ = component._route(make_message('Сосурян !WHO Nick'))
        assert entry.handler is handle_who
        assert entry.extract_args(prompt) == 'nick'

    def test_command_after_the_call_word(self, component):
        entry, prompt, addressed = component._route(make_message('сосурян !who nick'))
        assert entry.handler is handle_who
        assert addressed is True
        assert entry.extract_args(prompt) == 'nick'


def test_route_is_a_function_of_the_text(component):
    """No bot and no message needed: the same text routes the same way for any nick."""
    registry = component._registry
    assert route('@other привет', registry, 'other', False)[1:] == ('привет', True)
    assert route('@other привет', registry, 'sosuryan_bot', False) == (None, None, False)
    assert route('а почему', registry, 'sosuryan_bot', True) == (None, 'а почему', True)


def test_original_args_restore_the_case():
    ctx = CommandContext(
        message=make_message('!ask Что такое РФ'), user='gop', prompt='!ask что такое рф',
        original_text='!ask Что такое РФ', session_id='s', bot=FakeBot(), kind=Kind.GEMINI,
        args='что такое рф',
    )
    assert ctx.original_args == 'Что такое РФ'


async def test_two_quick_messages_start_one_generation(db, monkeypatch):
    """twitchio runs every event in its own task: the cooldown must be taken before the
    first await after its check, or both messages pass it."""
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    component = ChatComponent(FakeBot())
    handler = AsyncMock()
    monkeypatch.setattr('src.twitch.core.component.handle_default', handler)

    first, second = make_message('сосурян раз'), make_message('сосурян два')
    await asyncio.gather(component.event_message(first), component.event_message(second))

    assert handler.await_count == 1
    assert await count_bot_uses('viewer', Kind.GEMINI, 60) == 1
    refused = first if handler.await_args.args[0].message is second else second
    refused.respond.assert_awaited_once_with('texts.cooldown_gemini')


async def test_a_repeated_delivery_runs_the_command_once(db, monkeypatch):
    """Two chat subscriptions or an EventSub redelivery bring the same message twice:
    one !roll must not become two throws."""
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    component = ChatComponent(FakeBot())
    entry = component._registry.resolve('!roll')
    handler = AsyncMock()
    monkeypatch.setattr(entry, 'handler', handler)

    message = make_message('!roll')
    await asyncio.gather(component.event_message(message), component.event_message(message))
    await component.event_message(make_message('!roll', make_chatter('other')))

    assert handler.await_count == 2


async def test_roll_with_anything_after_it_is_refused_without_a_throw(db, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    bot = FakeBot()
    component = ChatComponent(bot)
    throw = AsyncMock()
    monkeypatch.setattr('src.twitch.local.roll.game.free_throw', throw)

    message = make_message('!roll 100 ПЛИЗ')
    await component.event_message(message)

    throw.assert_not_awaited()
    message.respond.assert_awaited_once_with('texts.roll_bad_input')
    assert bot.cooldown_remaining('viewer', Kind.LOCAL) == 0


async def test_a_failing_quota_check_gives_the_cooldown_back(db, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    monkeypatch.setattr(Quota, 'CHANNEL_PER_HOUR', 10)
    bot = FakeBot()
    component = ChatComponent(bot)
    monkeypatch.setattr('src.twitch.core.component.count_channel_bot_uses', AsyncMock(side_effect=RuntimeError('db')))
    with pytest.raises(RuntimeError):
        await component.event_message(make_message('сосурян раз'))
    assert bot.cooldown_remaining('viewer', Kind.GEMINI) == 0


async def test_quota_refusal_gives_the_cooldown_back(db, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    monkeypatch.setattr(Quota, 'FOLLOWER_PER_HOUR', 1)
    await record_bot_use('viewer', Kind.GEMINI)
    bot = FakeBot()
    component = ChatComponent(bot)
    message = make_message('сосурян раз')
    await component.event_message(message)
    message.respond.assert_awaited_once_with('texts.quota_exceeded texts.sub_hint')
    assert bot.cooldown_remaining('viewer', Kind.GEMINI) == 0
