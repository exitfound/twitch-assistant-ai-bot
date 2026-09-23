"""Maintenance commands that touch files: lore parsers and the emote merge into CONTENT.md."""
import json
import shutil
from pathlib import Path

import pytest

from src.cli import emotes, knowledge, main, probe
from src.cli.knowledge import LoreError, clear_knowledge, import_entries, lore_sources, parse_lore_file
from src.core import content, database
from src.core.database import save_chat_message
from src.gemini.memory import storage
from src.gemini.memory.storage import Block

ROOT = Path(__file__).resolve().parents[1]


def test_lines_format_skips_comments_and_blanks(tmp_path):
    path = tmp_path / 'lore.txt'
    path.write_text('# comment\nпервая\n\n  вторая  \n', encoding='utf-8')
    assert parse_lore_file(str(path)) == (['первая', 'вторая'], 'lore.txt')


def test_telegram_export(tmp_path):
    path = tmp_path / 'result.json'
    path.write_text(json.dumps({'name': 'Чат', 'messages': [
        {'type': 'message', 'from': 'gop', 'text': 'привет'},
        {'type': 'message', 'from': 'm1', 'text': ['ссылка ', {'type': 'link', 'text': 'x.ru'}]},
        {'type': 'service', 'actor': 'gop', 'text': 'joined'},
        {'type': 'message', 'from': 'gop', 'text': ''},
    ]}), encoding='utf-8')
    assert parse_lore_file(str(path), 'telegram') == (['gop: привет', 'm1: ссылка x.ru'], 'telegram:Чат')


def test_telegram_rejects_other_json(tmp_path):
    path = tmp_path / 'x.json'
    path.write_text('{"foo": 1}', encoding='utf-8')
    with pytest.raises(LoreError):
        parse_lore_file(str(path), 'telegram')


def test_text_format_cuts_into_short_pieces(tmp_path):
    path = tmp_path / 'article.md'
    path.write_text(
        '# Заголовок\n\nРаз. Два. Три. Четыре. Пять.\n\n```\ncode here\n```\n\n- **Пункт** [ссылка](http://x)\n',
        encoding='utf-8',
    )
    entries, _ = parse_lore_file(str(path), 'text')
    assert entries == ['Заголовок', 'Раз. Два. Три.', 'Четыре. Пять.', 'Пункт ссылка']


async def test_import_and_clear_by_source(db):
    assert await import_entries(['a', 'b', 'a'], 'one.txt') == (2, 1)
    await import_entries(['c'], 'two.txt')
    assert dict(await lore_sources()) == {'one.txt': 2, 'two.txt': 1}
    await clear_knowledge('one.txt')
    assert dict(await lore_sources()) == {'two.txt': 1}


def test_emote_merge_adds_and_keeps_the_file_whole(tmp_path, monkeypatch):
    path = tmp_path / 'CONTENT.md'
    shutil.copy(ROOT / 'docs' / 'CONTENT.md', path)
    monkeypatch.setattr(emotes, 'CONTENT_PATH', path)
    before = content.parse(path.read_text(encoding='utf-8'))['lists']
    existing = content._lines(before['emotes'])[0]

    added, skipped = emotes.merge({'Тестовые': ['BrandNewEmote', existing]})
    assert (added, skipped) == (['BrandNewEmote'], [existing])
    after = content.parse(path.read_text(encoding='utf-8'))['lists']
    assert set(after) == set(before)
    assert 'BrandNewEmote' in content._lines(after['emotes'])
    assert after['follow'] == before['follow']


def test_emote_replace_keeps_the_multiline_note(tmp_path, monkeypatch):
    """Replace mode rebuilds the list but keeps the note: losing its closing `-->` would
    comment out every key down to the next note, and the bot would not start."""
    path = tmp_path / 'CONTENT.md'
    shutil.copy(ROOT / 'docs' / 'CONTENT.md', path)
    monkeypatch.setattr(emotes, 'CONTENT_PATH', path)
    before = content.parse(path.read_text(encoding='utf-8'))

    emotes.merge({'Тестовые': ['OnlyEmote']}, replace=True)
    after = content.parse(path.read_text(encoding='utf-8'))
    assert content._lines(after['lists']['emotes']) == ['OnlyEmote']
    for section, keys in before.items():
        assert set(after[section]) == set(keys), section
    assert after['lists']['follow'] == before['lists']['follow']
    assert '<!--' in path.read_text(encoding='utf-8').split('### emotes')[1].split('###')[0]


def test_emote_merge_dry_run_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / 'CONTENT.md'
    shutil.copy(ROOT / 'docs' / 'CONTENT.md', path)
    monkeypatch.setattr(emotes, 'CONTENT_PATH', path)
    original = path.read_text(encoding='utf-8')
    emotes.merge({'Тестовые': ['BrandNewEmote']}, write=False)
    assert path.read_text(encoding='utf-8') == original


async def test_clear_memory_dry_run_deletes_nothing(db, capsys):
    """--dry-run only counts: a real wipe stops the memory until a paid --build-memory."""
    block = Block('2026-09-22 20:00', 1, 100, 60)
    await storage.save_chronicle(block, 'хроника', storage.ChronicleStatus.OK, [('gop', 'событие')])
    await storage.mark_built()

    await main.clear_memory(dry_run=True)
    assert 'chronicles 1' in capsys.readouterr().out
    # A CLI command closes the database when done; reopen it the way the next one would
    await database.init_db()
    assert await storage.memory_built()
    assert await storage.memory_counts() == {
        'chronicles': 1, 'chatter_events': 1, 'chatter_profiles': 0, 'memory_state': 1,
    }

    await main.clear_memory(dry_run=False)
    await database.init_db()
    assert not await storage.memory_built()
    assert set((await storage.memory_counts()).values()) == {0}


async def test_lore_import_commits_in_batches(db, monkeypatch):
    """One transaction for a big import held the write lock for ~16 s, and the running
    bot's chat inserts failed with «database is locked» after SQLite's 5 s wait."""
    monkeypatch.setattr(knowledge, 'IMPORT_BATCH', 2)
    commits = 0
    real_commit = db.commit

    async def counting_commit():
        nonlocal commits
        commits += 1
        await real_commit()
    monkeypatch.setattr(db, 'commit', counting_commit)
    assert await import_entries(['a', 'b', 'c', 'a', 'd'], 'x.txt') == (4, 1)
    assert commits == 3


@pytest.mark.parametrize('argv', [
    ['--dry-run'],
    ['--limit', '5'],
    ['--source', 'x.txt'],
    ['--replace-emotes'],
    ['--list-facts', '--vacuum'],
    ['--backup', '--sync-emotes'],
])
def test_cli_refuses_what_would_start_a_second_bot(argv):
    """With no action, main() returns False and bot.py starts the bot: a stray modifier
    brought up a second instance next to the running one, answering chat twice."""
    with pytest.raises(SystemExit):
        main.main(argv)


@pytest.mark.parametrize('argv', [
    ['--vacuum', '--dry-run'],
    ['--probe-context', '--dry-run'],
    ['--build-memory', '--replace-emotes'],
    ['--backup', '--source', 'x.txt'],
])
def test_cli_refuses_a_modifier_of_another_command(argv):
    """--vacuum --dry-run would compact the file for real, and --probe-context --dry-run
    would still pay for Gemini: a modifier the command ignores is refused."""
    with pytest.raises(SystemExit):
        main.main(argv)


def test_no_arguments_mean_run_the_bot():
    assert main.main([]) is False


@pytest.mark.parametrize(('fmt', 'content'), [
    ('lines', 'привет'.encode('cp1251')),
    ('text', 'привет'.encode('cp1251')),
    ('telegram', b'[1, 2, 3]'),
    ('telegram', json.dumps({'messages': ['not a dict']}).encode()),
    ('telegram', json.dumps({'chats': {'list': [{'messages': [{'type': 'message', 'text': 5}]}, 'x']}}).encode()),
])
def test_malformed_lore_is_a_clear_error(tmp_path, fmt, content):
    """A wrong encoding or an unexpected export shape printed a raw traceback."""
    path = tmp_path / 'lore.bin'
    path.write_bytes(content)
    with pytest.raises(LoreError):
        parse_lore_file(str(path), fmt)


async def test_probe_takes_questions_with_an_exclamation_mark(db):
    """Only commands are skipped: a question that merely ends in «!» is still a question."""
    await save_chat_message('s', 'gop', 'сосурян ну ты даёшь, расскажи про стрим!', addressed=True)
    await save_chat_message('s', 'gop', 'сосурян !who кто-то там из чата сегодня', addressed=True)
    await save_chat_message('s', 'gop', '!ask что такое терраформ и зачем он', addressed=True)
    questions = await probe._questions([], 10)
    assert [q.text for q in questions] == ['сосурян ну ты даёшь, расскажи про стрим!']
