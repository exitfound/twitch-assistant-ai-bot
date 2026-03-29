import argparse
import asyncio
import logging

from src.database import init_db, close_db, get_db
from src.knowledge import parse_lore_file, dedup_entries, clear_knowledge, import_entries


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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    if args.list_facts:
        asyncio.run(list_facts())
    elif args.upload_lore or args.clear_lore:
        if args.clear_lore and not args.upload_lore:
            asyncio.run(clear_lore())
        else:
            asyncio.run(upload_lore(args.upload_lore, args.clear_lore, args.dry_run))
    else:
        return False
    return True
