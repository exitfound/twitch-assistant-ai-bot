"""Output helpers from src/core/utils.py: what every chat line goes through."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.utils import NICK_MAX, clean_nick, defuse, gather_cancelling, reply, reply_to_bot, safe_format
from src.gemini.output import (
    EM_DASH, EN_DASH, cleanup_response, find_banned, fix_dashes, split_into_chunks, strip_links, strip_markdown,
    strip_pings, trim_to_sentence,
)


class TestTrimToSentence:
    def test_short_text_is_untouched(self):
        assert trim_to_sentence('Коротко.', 100) == 'Коротко.'

    def test_cuts_at_the_last_sentence_end(self):
        text = 'Первое предложение тут. Второе предложение уже не влезает целиком'
        assert trim_to_sentence(text, 40) == 'Первое предложение тут.'

    def test_dot_inside_a_date_is_not_a_sentence_end(self):
        text = 'Встреча 23.06 прошла отлично и потом было ещё много всего'
        result = trim_to_sentence(text, 30)
        assert result.endswith('…')
        assert '23.' in result
        assert len(result) <= 30

    def test_no_sentence_end_cuts_at_a_word(self):
        result = trim_to_sentence('слово ' * 20, 30)
        assert result.endswith('слово…')
        assert len(result) <= 30


class TestCleanupResponse:
    def test_leading_nick_of_the_asker_is_removed(self):
        assert cleanup_response('@gop привет', 'gop') == 'привет'

    def test_separator_after_the_nick_goes_with_it(self):
        assert cleanup_response(f'@gop {EN_DASH} текст', 'gop') == 'текст'
        assert cleanup_response('@gop: текст', 'gop') == 'текст'

    def test_a_longer_nick_is_not_eaten(self):
        assert cleanup_response('@gop5ter привет', 'gop') == '@gop5ter привет'

    def test_negative_number_survives(self):
        assert cleanup_response('@gop -5 очков', 'gop') == '-5 очков'

    def test_further_mentions_keep_the_nick_without_ping(self):
        assert cleanup_response(f'Победитель {EN_DASH} @gop', 'gop') == f'Победитель {EN_DASH} gop'

    def test_markdown_and_em_dash_are_cleaned(self):
        assert cleanup_response(f'*жирный* `код` {EM_DASH} да', 'x') == f'жирный код {EN_DASH} да'

    def test_long_answer_is_trimmed(self):
        assert len(cleanup_response('Фраза. ' * 200, 'x', max_len=100)) <= 100


def test_strip_markdown_keeps_underscores_in_nicks():
    assert strip_markdown('**m1ndsh1ft_** и ## limbo___') == 'm1ndsh1ft_ и limbo___'


def test_split_into_chunks_keeps_every_word():
    text = ' '.join(f'слово{i}' for i in range(200))
    chunks = split_into_chunks(text, 100, 10_000)
    assert all(len(c) <= 100 for c in chunks)
    assert ' '.join(chunks) == text


@pytest.mark.parametrize(('raw', 'nick'), [
    ('@Nick,', 'nick'),
    ('nick!?', 'nick'),
    ('x' * 40, 'x' * NICK_MAX),
])
def test_clean_nick(raw, nick):
    assert clean_nick(raw) == nick


def test_safe_format_survives_a_missing_placeholder():
    assert safe_format('@{user} {value}', user='gop') == '@{user} {value}'
    assert safe_format('@{user}', user='gop', extra=1) == '@gop'


@pytest.mark.parametrize(('raw', 'safe'), [('/ban x', 'ban x'), ('.me', 'me'), ('ok', 'ok')])
def test_defuse(raw, safe):
    assert defuse(raw) == safe


def test_strip_links_and_pings():
    assert strip_links('смотри https://evil.com/x и evil.ru сюда') == 'смотри и сюда'
    assert strip_pings('@gop привет @m1ndsh1ft_') == 'gop привет m1ndsh1ft_'


def test_find_banned_is_case_insensitive():
    assert find_banned('Плохое СЛОВО тут', ['слово']) == 'слово'
    assert find_banned('чисто', ['слово']) is None
    assert find_banned('что угодно', []) is None


def test_fix_dashes():
    assert fix_dashes(f'а {EM_DASH} б') == f'а {EN_DASH} б'


class TestReplyToBot:
    def reply(self, parent_id: str, body='реплика бота'):
        return SimpleNamespace(reply=SimpleNamespace(
            parent_user=SimpleNamespace(id=parent_id), parent_message_body=body,
        ))

    def test_reply_to_the_bot(self):
        assert reply_to_bot(self.reply('1000'), 1000) == 'реплика бота'

    def test_reply_to_someone_else(self):
        assert reply_to_bot(self.reply('42'), '1000') is None

    def test_not_a_reply(self):
        assert reply_to_bot(SimpleNamespace(reply=None), '1000') is None


class TestGatherCancelling:
    async def test_results_in_order(self):
        async def value(v):
            return v
        assert await gather_cancelling(value(1), value(2)) == [1, 2]

    async def test_first_error_cancels_the_rest_and_keeps_its_type(self):
        """gather() leaves the siblings running after an error: in the CLI they reopened
        the database after close_db() and the process never exited."""
        cancelled = asyncio.Event()

        async def fails():
            await asyncio.sleep(0)
            raise ValueError('boom')

        async def slow():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with pytest.raises(ValueError, match='boom'):
            await gather_cancelling(fails(), slow())
        assert cancelled.is_set()


async def test_reply_swallows_a_send_error():
    """A refusal or a fallback line must not fail the handler sending it."""
    message = SimpleNamespace(respond=AsyncMock(side_effect=RuntimeError('dns')))
    assert not await reply(message, 'текст')


async def test_reply_reports_a_message_twitch_dropped():
    dropped = SimpleNamespace(sent=False, dropped_code='automod')
    assert not await reply(SimpleNamespace(respond=AsyncMock(return_value=dropped)), 'текст')
    assert await reply(SimpleNamespace(respond=AsyncMock(return_value=SimpleNamespace(sent=True))), 'текст')
