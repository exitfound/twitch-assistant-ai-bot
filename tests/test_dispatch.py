"""Command matching and routing: which message reaches which handler, and on what text."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fakes import FakeBot, make_chatter, make_message
from src.core.commands import CommandContext, CommandEntry, Kind
from src.core.component import ChatComponent, route
from src.core.config import Follow, Quota
from src.core.database import count_bot_uses, record_bot_use
from src.gemini.commands import handle_who
from src.local.commands import handle_help, handle_stats
from src.local.roll.command import handle_roll, handle_rollstat


async def _noop(ctx):
    pass


class TestCommandEntry:
    def test_exact_entry_matches_only_itself(self):
        entry = CommandEntry('!roll', _noop, prefix=False, role=None, kind='local')
        assert entry.match('!roll')
        assert not entry.match('!roll 5')
        assert entry.extract_args('!roll') == ''

    def test_prefix_entry_needs_a_word_boundary(self):
        entry = CommandEntry('!who', _noop, prefix=True, role=None, kind='local')
        assert entry.match('!who')
        assert entry.match('!who nick')
        assert not entry.match('!whoever')

    @pytest.mark.parametrize('prompt', ['!ask: вопрос', '!ask:вопрос', '!ask, вопрос', '!ask вопрос'])
    def test_separators_before_args(self, prompt):
        entry = CommandEntry('!ask', _noop, prefix=True, role=None, kind='local')
        assert entry.match(prompt)
        assert entry.extract_args(prompt) == 'вопрос'


@pytest.fixture
def component():
    return ChatComponent(FakeBot())


@pytest.mark.parametrize(('text', 'handler'), [
    ('!help-bot', handle_help),
    ('!stat', handle_stats),
    ('!stat nick', handle_stats),
    ('!rollstat', handle_rollstat),
    ('!roll', handle_roll),
    ('!who nick', handle_who),
])
def test_registry_resolves_the_real_commands(component, text, handler):
    assert component._registry.resolve(text).handler is handler


@pytest.mark.parametrize('text', ['!whoever', '!roll 5', '!rolls', 'привет'])
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
    entry = component._registry.resolve('!ask')
    handler = AsyncMock()
    monkeypatch.setattr(entry, 'handler', handler)

    first, second = make_message('!ask раз'), make_message('!ask два')
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


async def test_quota_refusal_gives_the_cooldown_back(db, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    monkeypatch.setattr(Quota, 'FOLLOWER_PER_HOUR', 1)
    await record_bot_use('viewer', Kind.GEMINI)
    bot = FakeBot()
    component = ChatComponent(bot)
    await component.event_message(make_message('!ask раз'))
    assert bot.cooldown_remaining('viewer', Kind.GEMINI) == 0
