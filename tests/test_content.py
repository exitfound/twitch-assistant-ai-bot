"""CONTENT.md: parsing, hot reload, and the startup check of the real file."""
import os
from pathlib import Path

import pytest

from src.core import content
from src.core.content import Content, parse, validate_content
from src.local.roll import game, redemption, rewards

ROOT = Path(__file__).resolve().parents[1]


def test_parse_sections_keys_and_notes():
    raw = '\n'.join([
        '# Title, not a section',
        '## texts',
        'Prose before the first key is a note.',
        '### hello',
        'Привет, {user}!',
        '<!-- a note',
        '     spanning lines -->',
        'second line',
        '### bye',
        'Пока',
        '## lists',
        '### emotes',
        'Kappa',
    ])
    assert parse(raw) == {
        'texts': {'hello': 'Привет, {user}!\nsecond line', 'bye': 'Пока'},
        'lists': {'emotes': 'Kappa'},
    }


def test_duplicate_key_last_wins():
    assert parse('## texts\n### a\nfirst\n### a\nsecond')['texts']['a'] == 'second'


def test_accessors_substitute_and_split():
    assert Content.text('help') == 'texts.help'
    assert Content.items('follow') == ['follow {user}']
    assert Content.items('emotes') == []


def test_unknown_key_is_empty_not_a_crash():
    assert Content.text('no_such_key') == ''


def test_file_without_sections_keeps_the_previous_version():
    path = content.CONTENT_PATH
    assert Content.text('help') == 'texts.help'
    path.write_text('всё стёрто', encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert Content.text('help') == 'texts.help'


def test_half_written_file_keeps_the_previous_version():
    """An editor that saves in place can be caught mid-write: texts missing from that
    version would go to chat empty until the next save."""
    path = content.CONTENT_PATH
    assert Content.text('help') == 'texts.help'
    full = path.read_text(encoding='utf-8')
    path.write_text(full[:len(full) // 2], encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert Content.text('who_failed') == 'texts.who_failed'


def test_hot_reload_picks_up_an_edit():
    path = content.CONTENT_PATH
    Content.text('help')
    path.write_text(path.read_text(encoding='utf-8').replace('texts.help', 'новая справка'), encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert Content.text('help') == 'новая справка'


def test_missing_required_key_aborts_startup():
    path = content.CONTENT_PATH
    path.write_text(path.read_text(encoding='utf-8').replace('### help\n', '### helpp\n'), encoding='utf-8')
    with pytest.raises(ValueError, match=r'texts\.help'):
        validate_content()


def test_the_real_content_file_is_complete(monkeypatch):
    """The cheapest check in the project: a text added to CONTENT.md but not to REQUIRED,
    or a key renamed by a typo, fails here instead of at the next bot start."""
    real = ROOT / 'docs' / 'CONTENT.md'
    monkeypatch.setattr(content, 'CONTENT_PATH', real)
    monkeypatch.setattr(content, '_content', content._ContentFile(real))
    validate_content()
    data = content._content.get()
    unknown = [f'{s}.{k}' for s, keys in data.items() for k in keys
               if s != content.CHANNEL_SECTION and k not in content.REQUIRED.get(s, ())]
    assert unknown == []


@pytest.fixture
def real_content(monkeypatch):
    """The real CONTENT.md: the stand-in one holds key names and has no placeholders."""
    real = ROOT / 'docs' / 'CONTENT.md'
    monkeypatch.setattr(content, 'CONTENT_PATH', real)
    monkeypatch.setattr(content, '_content', content._ContentFile(real))


def test_reward_titles_and_descriptions_fit_twitch(real_content, monkeypatch):
    """Twitch takes a title up to 45 characters and a description up to 200, and the bot
    cuts anything longer mid-word: the check runs on the uncut text."""
    title_max, prompt_max = rewards.TITLE_MAX, rewards.PROMPT_MAX
    monkeypatch.setattr(rewards, 'TITLE_MAX', 10_000)
    monkeypatch.setattr(rewards, 'PROMPT_MAX', 10_000)
    for spec in rewards._specs():
        assert 0 < len(spec.title) <= title_max, spec.action
        assert len(spec.prompt) <= prompt_max, (spec.action, len(spec.prompt))
        assert '{' not in spec.prompt, spec.prompt


_FULL = {'target': 'victim', 'old_value': 10, 'value': 20, 'free_left': 2, 'loser': ('loser', 3),
         'champion': ('champ', 99), 'ceiling': 75, 'next_ceiling': 65, 'curse_minutes_left': 12,
         'protect_minutes_left': 7, 'limit': 5}


@pytest.mark.parametrize('action', list(game.Action))
def test_every_reward_outcome_fills_its_template(real_content, action):
    """A placeholder the call does not pass sends the raw template to chat, and the tests
    on the stand-in CONTENT.md cannot see it. Every success variant and every refusal."""
    outcomes = [game.Outcome(game.Status.OK, **{**_FULL, **change})
                for change in ({}, {'ceiling': None}, {'old_value': None}, {'target': 'gop'})]
    outcomes += [game.Outcome(status, **_FULL) for status in redemption._REFUND]
    for outcome in outcomes:
        text = redemption._render(action, 'gop', 'victim', outcome)
        assert text and '{' not in text, (outcome.status, text)
    for key in ('reward_refund_offline', 'reward_error'):
        assert '{' not in Content.text(key, user='gop', reward=rewards.reward_title(action))


def test_the_chill_text_fills_its_template(real_content):
    text = Content.text('roll_too_fast', user='gop', minutes=3)
    assert '3' in text and '{' not in text
