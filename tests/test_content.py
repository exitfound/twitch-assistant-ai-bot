"""CONTENT.md: parsing, hot reload, and the startup check of the real file."""
import os
from pathlib import Path

import pytest

from src.core import content
from src.core.content import Content, parse, validate_content

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
    real = ROOT / 'CONTENT.md'
    monkeypatch.setattr(content, 'CONTENT_PATH', real)
    monkeypatch.setattr(content, '_content', content._ContentFile(real))
    validate_content()
    data = content._content.get()
    unknown = [f'{s}.{k}' for s, keys in data.items() for k in keys if k not in content.REQUIRED.get(s, ())]
    assert unknown == []
