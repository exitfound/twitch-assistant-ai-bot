import argparse
import asyncio
import time

from src.cli.emotes import SOURCES, SyncError, fetch, merge
from src.cli.knowledge import (
    FORMATS, LoreError, clear_knowledge, count_knowledge, import_entries, lore_sources, parse_lore_file,
)
from src.core.database import backup_db, close_db, get_db, init_db, vacuum_db
from src.core.logging_setup import setup_logging
from src.cli import memory, probe


async def upload_lore(files: list[str], clear: bool, dry_run: bool, fmt: str, source: str | None):
    """Import lore files. Every row remembers its source: --source, or the file
    name (the chat name for a Telegram export). With --clear-lore and --source only
    that source is deleted first, so one source can be re-imported."""
    all_entries: list[tuple[str, str]] = []
    for path in files:
        try:
            entries, default_source = parse_lore_file(path, fmt)
        except (OSError, LoreError) as e:
            print(f'Ошибка: {e}')
            return
        file_source = source or default_source
        print(f'{path}: {len(entries)} записей, источник «{file_source}»')
        all_entries.extend((entry, file_source) for entry in entries)

    # The first file that has a line keeps it
    seen: dict[str, str] = {}
    for entry, src in all_entries:
        seen.setdefault(entry, src)
    unique = list(seen.items())
    dupes = len(all_entries) - len(unique)
    if dupes:
        print(f'Дубликатов между файлами: {dupes}')

    if dry_run:
        print(f'\n--- Dry run: {len(unique)} уникальных записей ---')
        for i, (entry, _) in enumerate(unique[:20], 1):
            print(f'  {i}. {entry[:100]}{"..." if len(entry) > 100 else ""}')
        if len(unique) > 20:
            print(f'  ... и ещё {len(unique) - 20}')
        return

    await init_db()
    try:
        if clear:
            deleted = await clear_knowledge(source)
            print(f'Удалено из базы знаний: {deleted}' + (f' (источник «{source}»)' if source else ' (всё)'))
        by_source: dict[str, list[str]] = {}
        for entry, src in unique:
            by_source.setdefault(src, []).append(entry)
        for src, entries in by_source.items():
            added, skipped = await import_entries(entries, src)
            print(f'«{src}»: импортировано {added}, уже было в базе {skipped}')
    finally:
        await close_db()


async def list_lore_sources():
    await init_db()
    try:
        for source, count in await lore_sources():
            print(f'  {count:>7}  {source if source is not None else "(источник не записан)"}')
    finally:
        await close_db()


async def sync_emotes(sources: list[str], replace: bool, dry_run: bool):
    """Pull emotes from Twitch into CONTENT.md. No DB needed."""
    from src.core.config import Twitch
    try:
        groups = await fetch(sources, Twitch.CLIENT_ID, Twitch.CLIENT_SECRET, Twitch.CHANNEL)
    except SyncError as e:
        print(f'Ошибка: {e}')
        return
    except Exception as e:
        print(f'Сеть или Twitch недоступны: {type(e).__name__}: {e}')
        return

    total = sum(len(names) for names in groups.values())
    print(f'Источники: {", ".join(sources)} – получено {total} эмот(ов)')
    for group, names in groups.items():
        print(f'  {group}: {len(names)}')
    if not total:
        return

    try:
        added, skipped = merge(groups, replace=replace, write=not dry_run)
    except SyncError as e:
        print(f'Ошибка: {e}')
        return

    if dry_run:
        print(f'\n--- Dry run: было бы добавлено {len(added)}, уже есть {len(skipped)} ---')
    else:
        print(f'\nДобавлено: {len(added)}, уже было: {len(skipped)}')
    if added:
        print('  ' + ' '.join(added))
    if replace and not dry_run:
        print('Список пересобран заново – вручную добавленные эмоты удалены.')


async def list_facts():
    await init_db()
    try:
        db = await get_db()
        async with db.execute(
            'SELECT username, fact, created_at FROM facts ORDER BY username, id'
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            print('Фактов нет.')
            return
        current_user = None
        for username, fact, created_at in rows:
            if username != current_user:
                current_user = username
                print(f'\n  @{username}:')
            print(f'    - {fact}  ({created_at})')
        print(f'\nВсего: {len(rows)} фактов')
    finally:
        await close_db()


async def clear_lore(source: str | None, dry_run: bool):
    await init_db()
    try:
        if dry_run:
            # --dry-run only counts, it deletes nothing
            count = await count_knowledge(source)
            print(f'Dry run: было бы удалено {count}' + (f' (источник «{source}»)' if source else ' (всё)'))
            return
        deleted = await clear_knowledge(source)
        print(f'Удалено из базы знаний: {deleted}' + (f' (источник «{source}»)' if source else ' (всё)'))
    finally:
        await close_db()


async def build_memory(dry_run: bool, limit: int, clear: bool):
    await init_db()
    try:
        if clear and not dry_run:
            await memory.clear()
            print('Память очищена (хроники, события, профили)')
        await memory.build_memory(dry_run, limit)
    finally:
        await close_db()


async def probe_context(ids: list[int], limit: int, samples: int):
    await init_db()
    try:
        await probe.probe(ids, limit, samples)
    finally:
        await close_db()


async def clear_memory(dry_run: bool):
    await init_db()
    try:
        if dry_run:
            # --dry-run only counts, as with --clear-lore: a real wipe costs a paid --build-memory
            counts = await memory.counts()
            print('Dry run: было бы удалено ' + ', '.join(f'{t} {n}' for t, n in counts.items()))
            return
        await memory.clear()
        print('Память очищена (хроники, события, профили)')
    finally:
        await close_db()


async def backup(destination: str | None):
    await init_db()
    try:
        target = destination or f'chat_history.backup-{time.strftime("%Y%m%d-%H%M%S")}.db'
        path = await backup_db(target)
        print(f'Копия БД сохранена: {path}')
    finally:
        await close_db()


async def vacuum():
    await init_db()
    try:
        print('VACUUM...')
        await vacuum_db()
        print('Готово.')
    finally:
        await close_db()


def _check_combination(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """One action per run, and each modifier only with an action that reads it.

    Without an action bot.py starts the bot: a stray --dry-run would bring up a second
    instance next to the running one, answering chat twice. With the wrong action it is
    ignored: --vacuum --dry-run compacts the file, --probe-context --dry-run still pays.
    --build-memory with --clear-memory and --upload-lore with --clear-lore are one action each.
    """
    lore = '--upload-lore / --clear-lore'
    actions = [name for name, given in (
        ('--list-facts', args.list_facts),
        ('--probe-context', args.probe_context is not None),
        ('--build-memory', args.build_memory),
        ('--clear-memory', args.clear_memory and not args.build_memory),
        ('--backup', args.backup is not None),
        ('--vacuum', args.vacuum),
        ('--sync-emotes', args.sync_emotes is not None),
        ('--lore-sources', args.lore_sources),
        (lore, bool(args.upload_lore or args.clear_lore)),
    ) if given]
    if len(actions) > 1:
        parser.error(f'по одной команде за запуск, а указаны: {", ".join(actions)}')
    modifiers = [(name, readers) for name, given, readers in (
        ('--dry-run', args.dry_run, {'--build-memory', '--clear-memory', '--sync-emotes', lore}),
        ('--source', args.source is not None, {lore}),
        ('--format', args.format != 'lines', {lore}),
        ('--limit', args.limit != parser.get_default('limit'), {'--build-memory', '--probe-context'}),
        ('--samples', args.samples != parser.get_default('samples'), {'--probe-context'}),
        ('--replace-emotes', args.replace_emotes, {'--sync-emotes'}),
    ) if given]
    if modifiers and not actions:
        names = ', '.join(name for name, _ in modifiers)
        parser.error(f'{names} без команды: бот не запускается с этими флагами')
    stray = [name for name, readers in modifiers if actions[0] not in readers]
    if stray:
        parser.error(f'{", ".join(stray)} не относится к {actions[0]}')


def main(argv: list[str] | None = None) -> bool:
    """Run a maintenance command. False – no command given, bot.py starts the bot."""
    parser = argparse.ArgumentParser(description='Twitch AI Bot')
    parser.add_argument(
        '--upload-lore', nargs='+', metavar='FILE',
        help='Импорт лора из txt файлов (бот не запускается)',
    )
    parser.add_argument(
        '--clear-lore', action='store_true',
        help='Очистить базу знаний (с --upload-lore: перед импортом, без: только очистка). '
             'С --source – только этот источник',
    )
    parser.add_argument(
        '--format', choices=FORMATS, default='lines',
        help='С --upload-lore: lines – строка = запись (по умолчанию); telegram – JSON '
             'экспорта Telegram Desktop; text – txt/md, режется на куски по 1–3 предложения',
    )
    parser.add_argument(
        '--source', metavar='NAME',
        help='Источник записей: с --upload-lore – под каким именем записать (по умолчанию '
             'имя файла, для Telegram – имя чата); с --clear-lore – что удалить',
    )
    parser.add_argument(
        '--lore-sources', action='store_true',
        help='Показать источники базы знаний и сколько в каждом записей',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Показать что будет импортировано (без записи в БД)',
    )
    parser.add_argument(
        '--list-facts', action='store_true',
        help='Показать все сохранённые факты из БД',
    )
    parser.add_argument(
        '--build-memory', action='store_true',
        help='Память бота по всей истории: хроники сессий и профили зрителей. '
             'Повторный запуск дописывает недостающее. С --dry-run ничего не пишет',
    )
    parser.add_argument(
        '--clear-memory', action='store_true',
        help='Стереть память (с --build-memory: перед сборкой, без: только очистка)',
    )
    parser.add_argument(
        '--probe-context', nargs='*', type=int, metavar='ID',
        help='Замер контекста: реальные обращения к боту (id из chat_messages, по умолчанию '
             '--limit последних) в вариантах now (узкое окно чата) и new (то, что бот отправляет). Стоит денег',
    )
    parser.add_argument(
        '--samples', type=int, default=1, metavar='N',
        help='С --probe-context: сколько ответов на каждый вариант (температура высокая)',
    )
    parser.add_argument(
        '--limit', type=int, default=3, metavar='N',
        help='С --build-memory --dry-run: сколько хроник и профилей показать; '
             'с --probe-context: сколько последних обращений взять (по умолчанию 3)',
    )
    parser.add_argument(
        '--backup', nargs='?', const='', metavar='FILE',
        help='Согласованная копия БД (по умолчанию chat_history.backup-<дата>.db)',
    )
    parser.add_argument(
        '--vacuum', action='store_true',
        help='Уплотнить файл БД (VACUUM)',
    )
    parser.add_argument(
        '--sync-emotes', nargs='*', metavar='SOURCE',
        help=f'Подтянуть эмоты из Twitch в CONTENT.md. Источники: {", ".join(SOURCES)} '
             f'(по умолчанию channel). Работает с --dry-run',
    )
    parser.add_argument(
        '--replace-emotes', action='store_true',
        help='С --sync-emotes: пересобрать список с нуля вместо слияния',
    )
    args = parser.parse_args(argv)
    _check_combination(parser, args)

    if args.list_facts:
        setup_logging('WARNING')
        asyncio.run(list_facts())
    elif args.probe_context is not None:
        setup_logging('WARNING')
        asyncio.run(probe_context(args.probe_context, max(1, args.limit), max(1, args.samples)))
    elif args.build_memory:
        setup_logging('WARNING')
        asyncio.run(build_memory(args.dry_run, max(1, args.limit), args.clear_memory))
    elif args.clear_memory:
        setup_logging('WARNING')
        asyncio.run(clear_memory(args.dry_run))
    elif args.backup is not None:
        setup_logging('WARNING')
        asyncio.run(backup(args.backup or None))
    elif args.vacuum:
        setup_logging('WARNING')
        asyncio.run(vacuum())
    elif args.sync_emotes is not None:
        setup_logging('WARNING')
        asyncio.run(sync_emotes(args.sync_emotes or ['channel'], args.replace_emotes, args.dry_run))
    elif args.lore_sources:
        setup_logging('WARNING')
        asyncio.run(list_lore_sources())
    elif args.upload_lore or args.clear_lore:
        setup_logging('WARNING')
        if args.clear_lore and not args.upload_lore:
            asyncio.run(clear_lore(args.source, args.dry_run))
        else:
            asyncio.run(upload_lore(args.upload_lore, args.clear_lore, args.dry_run,
                                    args.format, args.source))
    else:
        return False
    return True
