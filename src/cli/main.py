import argparse
import asyncio
import time

from src.cli.emotes import SOURCES, SyncError, fetch, merge
from src.cli.knowledge import parse_lore_file, dedup_entries, clear_knowledge, import_entries
from src.core.database import backup_db, close_db, get_db, init_db, vacuum_db
from src.core.logging_setup import setup_logging


async def upload_lore(files: list[str], clear: bool, dry_run: bool):
    all_entries = []
    for path in files:
        entries = parse_lore_file(path)
        print(f'{path}: {len(entries)} записей')
        all_entries.extend(entries)

    unique = dedup_entries(all_entries)
    dupes = len(all_entries) - len(unique)
    if dupes:
        print(f'Дубликатов между файлами: {dupes}')

    if dry_run:
        print(f'\n--- Dry run: {len(unique)} уникальных записей ---')
        for i, entry in enumerate(unique[:20], 1):
            print(f'  {i}. {entry[:100]}{"..." if len(entry) > 100 else ""}')
        if len(unique) > 20:
            print(f'  ... и ещё {len(unique) - 20}')
        return

    await init_db()
    try:
        if clear:
            await clear_knowledge()
            print('База знаний очищена (knowledge + knowledge_fts)')
        if unique:
            added, skipped = await import_entries(unique)
            print(f'Импортировано: {added}, пропущено дублей в БД: {skipped}')
    finally:
        await close_db()


async def sync_emotes(sources: list[str], replace: bool, dry_run: bool):
    """Подтянуть эмоты из Twitch в CONTENT.md. БД не нужна."""
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
    print(f'Источники: {", ".join(sources)} — получено {total} эмот(ов)')
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
        print('Список пересобран заново — вручную добавленные эмоты удалены.')


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


async def clear_lore():
    await init_db()
    try:
        await clear_knowledge()
        print('База знаний очищена (knowledge + knowledge_fts)')
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


def main():
    parser = argparse.ArgumentParser(description='Twitch AI Bot')
    parser.add_argument(
        '--upload-lore', nargs='+', metavar='FILE',
        help='Импорт лора из txt файлов (бот не запускается)',
    )
    parser.add_argument(
        '--clear-lore', action='store_true',
        help='Очистить базу знаний (с --upload-lore: перед импортом, без: только очистка)',
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
    args = parser.parse_args()

    if args.list_facts:
        setup_logging('WARNING')
        asyncio.run(list_facts())
    elif args.backup is not None:
        setup_logging('WARNING')
        asyncio.run(backup(args.backup or None))
    elif args.vacuum:
        setup_logging('WARNING')
        asyncio.run(vacuum())
    elif args.sync_emotes is not None:
        setup_logging('WARNING')
        asyncio.run(sync_emotes(args.sync_emotes or ['channel'], args.replace_emotes, args.dry_run))
    elif args.upload_lore or args.clear_lore:
        setup_logging('WARNING')
        if args.clear_lore and not args.upload_lore:
            asyncio.run(clear_lore())
        else:
            asyncio.run(upload_lore(args.upload_lore, args.clear_lore, args.dry_run))
    else:
        return False
    return True
