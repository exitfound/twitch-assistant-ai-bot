"""bot.py --build-memory: the bot's memory over the whole history, once.

After that the bot keeps it up to date itself: memory_loop() writes every
conversation once the chat has gone quiet (src/gemini/memory/build.py). A rerun
only fills what is missing: conversations without a chronicle, failed chronicles,
chatters without a profile.
"""
import asyncio

from src.core.config import Memory
from src.core.utils import gather_cancelling
from src.gemini import client
from src.gemini.memory import build, storage

def _cost() -> str:
    u = client.usage
    dollars = client.cost_estimate(u['prompt'], u['output'])
    return f'токенов: вход {u["prompt"]:,}, выход {u["output"]:,} – примерно ${dollars:.2f}'.replace(',', ' ')


def _print_profile(profile: storage.Profile) -> None:
    print(f'\n=== {profile.username} (сессий: {profile.sessions_seen}) ===')
    print(profile.portrait)
    for r in profile.relations:
        print(f'  – {r["nick"]}: {r["note"]}')


def _big(blocks: list[storage.Block]) -> list[storage.Block]:
    return [b for b in blocks if b.count >= Memory.CONVERSATION_MIN_MESSAGES]


async def _chronicles(blocks: list[storage.Block], save: bool) -> dict[str, build.Chronicle | None]:
    """Chronicles in parallel – generate() caps the concurrency itself."""
    async def one(block: storage.Block) -> build.Chronicle | None:
        try:
            chronicle = await build.write_chronicle(block)
        except build.Unavailable:
            # Saved as failed: the next --build-memory retries it
            chronicle = None
        if save:
            await build.save_result(block, chronicle)
        print(f'  {block.key}: {"готово" if chronicle else "Gemini ничего не вернул"}')
        return chronicle

    results = await gather_cancelling(*(one(b) for b in blocks))
    return {b.key: c for b, c in zip(blocks, results, strict=True)}


async def build_memory(dry_run: bool, limit: int) -> None:
    # Nobody is waiting in chat: use every Gemini slot, not just the memory's share
    build.use_all_slots()
    blocks = await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=False)
    chatters = await storage.ever_active_chatters(_big(blocks), Memory.PROFILE_MIN_MESSAGES)
    print(f'Законченных разговоров (тишина {Memory.SILENCE_MINUTES}+ мин): {len(blocks)}, '
          f'с {Memory.CONVERSATION_MIN_MESSAGES}+ сообщениями: {len(_big(blocks))}; '
          f'зрителей с {Memory.PROFILE_MIN_MESSAGES}+ сообщениями хоть в одном: {len(chatters)}')

    if dry_run:
        await _preview(_big(blocks), chatters, limit)
        return

    # A failed chronicle is retried: it may have been a Gemini outage, not a block
    failed = await storage.failed_blocks()
    if failed:
        print(f'\nХроники, которые не получились раньше: {len(failed)}')
        await _chronicles(failed, save=True)

    built = await storage.memory_built()
    todo = await storage.chat_blocks(Memory.SILENCE_MINUTES, uncovered=True)
    print(f'\nНовых разговоров: {len(todo)}, из них для хроники: {len(_big(todo))}')
    if built:
        # Profiles already exist: each conversation updates them in order, as the bot does
        for block in todo:
            if not await build.process_block(block):
                print('Gemini не отвечает – остальное в следующий раз')
                break
    else:
        # The history in one go; a build killed halfway just goes on from here next time
        await _chronicles(_big(todo), save=True)
        for block in todo:
            if block not in _big(todo):
                await storage.save_chronicle(block, '', storage.ChronicleStatus.SKIPPED, [])

    missing = [u for u in chatters if await storage.get_profile(u) is None]
    print(f'\nПрофили: нужно {len(missing)}, уже есть {len(chatters) - len(missing)}')

    async def one(username: str) -> bool:
        try:
            profile = await build.first_profile(username, chatters[username])
        except build.Unavailable:
            profile = None
        print(f'  {username}: {"готово" if profile else "не получилось"}')
        return profile is not None

    done = await gather_cancelling(*(one(u) for u in missing))

    moved = await storage.move_unaddressed_facts()
    print(f'\nФакты без @ника перенесены в knowledge: {moved} новых строк')
    if not built:
        # From now on the bot keeps the memory itself
        await storage.mark_built()
    failed = len(await storage.failed_blocks())
    if failed or not all(done):
        print(f'\nНе получилось: хроник {failed}, профилей {done.count(False)} – '
              f'повторный запуск --build-memory попробует их снова')
    print(f'\n{_cost()}')


async def _preview(blocks: list[storage.Block], chatters: dict[str, storage.Block], limit: int) -> None:
    """Nothing is written: the last `limit` chronicles and `limit` profiles, printed."""
    recent = blocks[-limit:]
    print(f'\n--- Dry run: хроники последних {len(recent)} разговоров ---')
    chronicles = await _chronicles(recent, save=False)
    for key, chronicle in chronicles.items():
        print(f'\n##### {key}')
        if chronicle is None:
            print('(пусто)')
            continue
        print(chronicle.text)
        for nick, event in chronicle.events:
            print(f'  • {nick}: {event}')

    # Events of the preview chronicles are not in the DB – hand them over directly
    events: dict[str, list[tuple[str, str]]] = {}
    for key, chronicle in chronicles.items():
        for nick, event in chronicle.events if chronicle else []:
            events.setdefault(nick, []).append((key, event))

    top = list(chatters)[:limit]
    print(f'\n--- Dry run: профили {len(top)} самых активных ---')
    profiles = await asyncio.gather(*(
        build.first_profile(u, chatters[u], save=False, extra_events=events.get(u, []))
        for u in top
    ), return_exceptions=True)
    for username, profile in zip(top, profiles, strict=True):
        if isinstance(profile, build.Unavailable):
            profile = None
        elif isinstance(profile, BaseException):
            raise profile
        if profile is None:
            print(f'\n=== {username}: не получилось ===')
        else:
            _print_profile(profile)
    print(f'\n{_cost()}')


async def clear() -> None:
    await storage.clear_memory()


async def counts() -> dict[str, int]:
    return await storage.memory_counts()
