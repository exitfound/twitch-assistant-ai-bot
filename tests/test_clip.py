"""!clip without Twitch: the length parser, the handler's answers and limit, the wait for the clip."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import twitchio

from fakes import FakeBot, make_chatter, make_message
from src.core.commands import CommandContext, Kind
from src.core.component import ChatComponent
from src.core.config import Clip, Follow
from src.core.database import count_bot_uses, record_bot_use
from src.local import clip


@pytest.mark.parametrize(('args', 'parsed'), [
    ('', (Clip.DEFAULT_SECONDS, '')),
    ('45', (45, '')),
    ('60', (60, '')),
    ('[Смешной момент]', (Clip.DEFAULT_SECONDS, 'Смешной момент')),
    ('30 [Смешной, момент!]', (30, 'Смешной, момент!')),
    ('30 [ с пробелами ]', (30, 'с пробелами')),
    ('30 [внутри [скобки] тоже]', (30, 'внутри [скобки] тоже')),
    ('30 []', (30, '')),
])
def test_length_and_title(args, parsed):
    assert clip.parse(args) == parsed


@pytest.mark.parametrize('args', ['4', '61', '0', '-5', '+30', '7.5', '30с', '30s', '999999', '30abc [x]'])
def test_a_length_that_is_not_bare_digits_in_range_is_refused(args):
    with pytest.raises(clip.BadArgs) as error:
        clip.parse(args)
    assert error.value.key == 'clip_bad_length'


@pytest.mark.parametrize('args', ['Смешной момент', '[незакрытая', 'закрытая]', '30 [x] хвост', '[x] 30', '[',
                                  '30 sakjf', '30 сек', '30 45', '30с момент'])
def test_a_title_outside_brackets_is_a_format_error(args):
    """«!clip 30 sakjf» is a length and a title without brackets: the answer must be about
    the brackets, not claim that «30 sakjf» is not a length."""
    with pytest.raises(clip.BadArgs) as error:
        clip.parse(args)
    assert error.value.key == 'clip_usage'


def test_a_long_title_is_cut():
    assert clip.parse('30 [' + 'а' * 300 + ']') == (30, 'а' * clip.TITLE_MAX)


async def test_a_clip_without_a_title_gets_a_numbered_one(db):
    await record_bot_use('someone', clip.KIND)
    await record_bot_use('someone', clip.KIND)
    ctx = _ctx('!clip 30')
    await clip.handle_clip(ctx)
    ctx.bot.create_clip.assert_awaited_once_with(30, 'texts.clip_title')


async def test_the_number_counts_every_clip_of_the_bot(db, monkeypatch):
    seen = []
    monkeypatch.setattr(clip.Content, 'text', lambda key, **values: seen.append(values) or key)
    await record_bot_use('someone', clip.KIND)
    await record_bot_use('other', clip.KIND)
    await record_bot_use('other', 'ascii')
    await clip.default_title('gop')
    assert seen[0]['number'] == 3
    assert seen[0]['user'] == 'gop'


def _ctx(text: str = '!clip 30', chatter=None, bot=None) -> CommandContext:
    message = make_message(text, chatter or make_chatter('gop', subscriber=True))
    return CommandContext(message=message, user=message.chatter.name, prompt=text.lower(),
                          original_text=text, session_id='2026-09-22 20:00', bot=bot or FakeBot(),
                          kind=Kind.LOCAL, args=text.lower()[len('!clip'):].strip())


def _http_error(status: int, message: str) -> twitchio.HTTPException:
    return twitchio.HTTPException('clip', status=status, extra=message)


async def test_a_clip_is_made_linked_and_counted(db):
    ctx = _ctx('!clip 45 [Смешной Момент]')
    await clip.handle_clip(ctx)
    ctx.bot.create_clip.assert_awaited_once_with(45, 'Смешной Момент')
    ctx.message.respond.assert_awaited_once_with('texts.clip_done')
    assert await count_bot_uses('gop', clip.KIND, 60) == 1


async def test_offline_there_is_nothing_to_clip(db):
    ctx = _ctx(bot=FakeBot(stream_live=False))
    await clip.handle_clip(ctx)
    ctx.bot.create_clip.assert_not_awaited()
    ctx.message.respond.assert_awaited_once_with('texts.clip_offline')


@pytest.mark.parametrize(('text', 'key'), [
    ('!clip 500', 'texts.clip_bad_length'),
    ('!clip 30с', 'texts.clip_bad_length'),
    ('!clip смешной момент', 'texts.clip_usage'),
])
async def test_malformed_args_are_refused_without_a_clip(db, text, key):
    bot = FakeBot()
    bot.set_cooldown('gop', 30, Kind.LOCAL)
    ctx = _ctx(text, bot=bot)
    await clip.handle_clip(ctx)
    bot.create_clip.assert_not_awaited()
    ctx.message.respond.assert_awaited_once_with(key)
    assert bot.cooldown_remaining('gop', Kind.LOCAL) == 0


async def test_the_limit_is_per_stream(db, monkeypatch):
    monkeypatch.setattr(Clip, 'PER_STREAM', 3)
    bot = FakeBot()
    for _ in range(3):
        await clip.handle_clip(_ctx(bot=bot))
    ctx = _ctx(bot=bot)
    await clip.handle_clip(ctx)
    assert bot.create_clip.await_count == 3
    ctx.message.respond.assert_awaited_once_with('texts.clip_no_left')


async def test_the_broadcaster_is_not_limited(db, monkeypatch):
    monkeypatch.setattr(Clip, 'PER_STREAM', 1)
    bot = FakeBot()
    for _ in range(3):
        await clip.handle_clip(_ctx(chatter=make_chatter('streamer', broadcaster=True), bot=bot))
    assert bot.create_clip.await_count == 3


@pytest.mark.parametrize(('status', 'message', 'key'), [
    (400, 'The title did not pass AutoMod checks.', 'texts.clip_bad_title'),
    (400, 'The category is not clippable.', 'texts.clip_rejected'),
    (403, 'The broadcaster has restricted the ability to capture clips.', 'texts.clip_rejected'),
    (404, 'The broadcaster must be broadcasting live.', 'texts.clip_offline'),
    (401, 'Missing scope: clips:edit', 'texts.clip_failed'),
    (500, 'Internal Server Error', 'texts.clip_failed'),
])
async def test_a_twitch_refusal_is_answered_and_not_counted(db, status, message, key):
    bot = FakeBot()
    bot.create_clip = AsyncMock(side_effect=_http_error(status, message))
    ctx = _ctx(bot=bot)
    await clip.handle_clip(ctx)
    ctx.message.respond.assert_awaited_once_with(key)
    assert await count_bot_uses('gop', clip.KIND, 60) == 0


async def test_a_clip_that_never_appears_is_a_failure(db):
    bot = FakeBot()
    bot.create_clip = AsyncMock(return_value=None)
    ctx = _ctx(bot=bot)
    await clip.handle_clip(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.clip_failed')
    assert await count_bot_uses('gop', clip.KIND, 60) == 0


async def test_an_unexpected_error_still_answers(db):
    bot = FakeBot()
    bot.create_clip = AsyncMock(side_effect=RuntimeError('network'))
    ctx = _ctx(bot=bot)
    await clip.handle_clip(ctx)
    ctx.message.respond.assert_awaited_once_with('texts.clip_failed')


async def test_a_viewer_without_badges_is_refused_by_the_gate(db, monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', False)
    bot = FakeBot()
    message = make_message('!clip 30')
    await ChatComponent(bot).event_message(message)
    bot.create_clip.assert_not_awaited()
    message.respond.assert_awaited_once_with('texts.role_denied_sub')


# --- waiting for Twitch to list the clip -------------------------------------

class _Twitch:
    """create_partialuser().create_clip() and fetch_clips(), with the clip listed on the n-th check."""

    def __init__(self, listed_on: int | None, *, failing_checks: int = 0) -> None:
        self.channel = SimpleNamespace(create_clip=AsyncMock(return_value=SimpleNamespace(id='Slug')))
        self.checks = 0
        self._listed_on = listed_on
        self._failing = failing_checks

    def create_partialuser(self, user_id):
        return self.channel

    async def fetch_clips(self, *, clip_ids):
        self.checks += 1
        if self.checks <= self._failing:
            raise RuntimeError('helix hiccup')
        if self._listed_on is not None and self.checks >= self._listed_on:
            return [SimpleNamespace(url=f'https://clips.twitch.tv/{clip_ids[0]}')]
        return []


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(clip.asyncio, 'sleep', AsyncMock())


async def test_the_link_comes_once_twitch_lists_the_clip(no_wait):
    twitch = _Twitch(listed_on=3)
    url = await clip.make_clip(twitch, '42', '1000', 30, 'момент')
    assert url == 'https://clips.twitch.tv/Slug'
    assert twitch.checks == 3
    twitch.channel.create_clip.assert_awaited_once_with(token_for='1000', duration=30, title='момент')


async def test_a_failed_check_is_not_a_failed_clip(no_wait):
    twitch = _Twitch(listed_on=2, failing_checks=1)
    assert await clip.make_clip(twitch, '42', '1000', 30, None) == 'https://clips.twitch.tv/Slug'


async def test_waiting_stops_after_a_minute(no_wait):
    twitch = _Twitch(listed_on=None)
    assert await clip.make_clip(twitch, '42', '1000', 30, None) is None
    assert twitch.checks == clip.CONFIRM_SECONDS // clip.POLL_SECONDS
