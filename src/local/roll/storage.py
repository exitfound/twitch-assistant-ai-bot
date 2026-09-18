"""Запросы игры к SQLite: rolls, rewards, roll_actions, roll_perks.

Сами таблицы и их миграции создаёт init_db() в src/core/database.py: схема
лежит в одном месте, так проще следить, что старт на живой базе идемпотентен.
"""
from typing import NamedTuple

from src.core.database import get_db


async def save_roll(
    session_id: str, username: str, value: int, *, free_throw: bool, limit: int | None = None,
) -> None:
    """Записать ролл. free_throw=True – бросок идёт в счёт бесплатных.

    Платный бросок, переброс и проклятие счётчик не трогают: жертва саботажа
    не должна терять свои бесплатные броски из-за чужих баллов.

    limit – сколько бесплатных бросков положено игроку по его статусу. Он
    известен только когда человек катает из чата: в событии выкупа награды
    значков нет, поэтому лимит запоминается в строке.
    """
    db = await get_db()
    await db.execute(
        'INSERT INTO rolls (session_id, username, roll_value, free_throws, free_limit)'
        ' VALUES (?, ?, ?, ?, ?)'
        ' ON CONFLICT(session_id, username) DO UPDATE SET roll_value = excluded.roll_value,'
        ' rolled_at = CURRENT_TIMESTAMP, free_throws = free_throws + excluded.free_throws,'
        ' free_limit = COALESCE(excluded.free_limit, free_limit)',
        (session_id, username, value, int(free_throw), limit),
    )
    await db.commit()


class RollRow(NamedTuple):
    value: int
    free_throws: int
    curse_ceiling: int | None
    curse_floor_at: float | None
    curse_until: float | None
    free_limit: int | None      # лимит по статусу, запомненный на броске из чата


async def get_roll(session_id: str, username: str) -> RollRow | None:
    """Строка игрока в сессии или None, если он не катал."""
    db = await get_db()
    async with db.execute(
        'SELECT roll_value, free_throws, curse_ceiling, curse_floor_at, curse_until, free_limit FROM rolls'
        ' WHERE session_id = ? AND username = ?',
        (session_id, username),
    ) as cursor:
        row = await cursor.fetchone()
    return RollRow(*row) if row else None


async def set_curse(
    session_id: str, username: str, ceiling: int | None, floor_at: float | None,
    until: float | None = None,
) -> None:
    db = await get_db()
    await db.execute(
        'UPDATE rolls SET curse_ceiling = ?, curse_floor_at = ?, curse_until = ?'
        ' WHERE session_id = ? AND username = ?',
        (ceiling, floor_at, until, session_id, username),
    )
    await db.commit()


async def get_expired_curses(session_id: str, floor_before: float, now: float) -> list[str]:
    """Кто в сессии всё ещё числится проклятым, хотя проклятие уже кончилось.

    Кончилось – это потолок встал на дно до floor_before или вышел жёсткий срок
    curse_until у проклятия залупы прошлого эфира.
    """
    db = await get_db()
    async with db.execute(
        'SELECT username FROM rolls WHERE session_id = ? AND curse_ceiling IS NOT NULL'
        ' AND ((curse_floor_at IS NOT NULL AND curse_floor_at <= ?)'
        ' OR (curse_until IS NOT NULL AND curse_until <= ?)) ORDER BY username',
        (session_id, floor_before, now),
    ) as cursor:
        return [username for (username,) in await cursor.fetchall()]


async def get_reward_ids() -> dict[str, str]:
    """{действие: id награды в Twitch} – какие награды бот уже создал."""
    db = await get_db()
    async with db.execute('SELECT action, reward_id FROM rewards') as cursor:
        rows = await cursor.fetchall()
    return {action: reward_id for action, reward_id in rows}


async def save_reward_id(action: str, reward_id: str) -> None:
    db = await get_db()
    await db.execute(
        'INSERT INTO rewards (action, reward_id) VALUES (?, ?)'
        ' ON CONFLICT(action) DO UPDATE SET reward_id = excluded.reward_id',
        (action, reward_id),
    )
    await db.commit()


async def get_action_status(redemption_id: str) -> str | None:
    """Итог уже обработанной награды или None, если её ещё не видели."""
    db = await get_db()
    async with db.execute(
        'SELECT status FROM roll_actions WHERE redemption_id = ?', (redemption_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def has_action(session_id: str, action: str, target: str, status: str) -> bool:
    db = await get_db()
    async with db.execute(
        'SELECT 1 FROM roll_actions'
        ' WHERE session_id = ? AND action = ? AND target = ? AND status = ? LIMIT 1',
        (session_id, action, target, status),
    ) as cursor:
        return await cursor.fetchone() is not None


async def seconds_since_action(session_id: str, action: str, target: str, status: str) -> int | None:
    """Сколько секунд прошло с последнего такого действия над целью. None – его не было.

    Время считает сама SQLite: created_at пишется её CURRENT_TIMESTAMP в UTC,
    и сравнивать его с часами Python значило бы следить за часовыми поясами.
    """
    db = await get_db()
    async with db.execute(
        "SELECT CAST(strftime('%s', 'now') - strftime('%s', MAX(created_at)) AS INTEGER)"
        ' FROM roll_actions WHERE session_id = ? AND action = ? AND target = ? AND status = ?',
        (session_id, action, target, status),
    ) as cursor:
        (seconds,) = await cursor.fetchone()
    return seconds


async def save_action(
    redemption_id: str, session_id: str, action: str, actor: str, user_input: str,
    target: str | None, old_value: int | None, new_value: int | None, status: str,
) -> None:
    db = await get_db()
    await db.execute(
        'INSERT INTO roll_actions (redemption_id, session_id, action, actor, user_input,'
        ' target, old_value, new_value, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (redemption_id, session_id, action, actor, user_input, target, old_value, new_value, status),
    )
    await db.commit()


async def get_session_loser(session_id: str) -> tuple[str, int] | None:
    """Returns (username, roll_value) of the current session залупа (minimum roll).

    При равных минимумах побеждает тот, кто откатал раньше. У rolled_at
    точность до секунды, поэтому финальный тайбрейк – по id: иначе два
    одинаковых ролла в одну секунду давали бы разный ответ от запроса
    к запросу.
    """
    db = await get_db()
    async with db.execute(
        'SELECT username, roll_value FROM rolls WHERE session_id = ?'
        ' ORDER BY roll_value ASC, rolled_at ASC, id ASC LIMIT 1',
        (session_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def get_session_champion(session_id: str) -> tuple[str, int] | None:
    """(username, roll_value) китежанина сессии – самого высокого ролла.

    Зеркало get_session_loser(): при равных максимумах титул у того, кто
    выбросил раньше, финальный тайбрейк – по id.
    """
    db = await get_db()
    async with db.execute(
        'SELECT username, roll_value FROM rolls WHERE session_id = ?'
        ' ORDER BY roll_value DESC, rolled_at ASC, id ASC LIMIT 1',
        (session_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def get_last_roll_session_before(session_id: str, before: float) -> str | None:
    """Последняя сессия с роллами, кроме этой, чьи броски закончились до before.

    Нужна только для первого эфира после перехода на сессии-эфиры: прошлого
    эфира в таблице streams ещё нет, и итоги берутся по старой сессии-дате.
    Броски, сделанные в идущем эфире ещё на старом коде, в итоги не попадают.
    """
    db = await get_db()
    async with db.execute(
        'SELECT session_id FROM rolls WHERE session_id != ? GROUP BY session_id'
        " HAVING CAST(strftime('%s', MAX(rolled_at)) AS REAL) < ?"
        ' ORDER BY MAX(rolled_at) DESC LIMIT 1',
        (session_id, before),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


# --- бонусы по итогам прошлого эфира -------------------------------------------

class PerkRow(NamedTuple):
    active_until: float | None
    consumed: bool


async def add_perk(session_id: str, username: str, perk: str, from_session: str) -> bool:
    """Выдать бонус. False – такой бонус в этой сессии уже выдан."""
    db = await get_db()
    cursor = await db.execute(
        'INSERT OR IGNORE INTO roll_perks (session_id, username, perk, from_session) VALUES (?, ?, ?, ?)',
        (session_id, username, perk, from_session),
    )
    await db.commit()
    return cursor.rowcount == 1


async def get_perk(session_id: str, username: str, perk: str) -> PerkRow | None:
    db = await get_db()
    async with db.execute(
        'SELECT active_until, consumed FROM roll_perks WHERE session_id = ? AND username = ? AND perk = ?',
        (session_id, username, perk),
    ) as cursor:
        row = await cursor.fetchone()
    return PerkRow(row[0], bool(row[1])) if row else None


async def activate_perks(session_id: str, username: str, now: float, until: float) -> list[str]:
    """Запустить отсчёт ещё не начатых бонусов игрока. Возвращает, какие запущены."""
    db = await get_db()
    async with db.execute(
        'SELECT perk FROM roll_perks WHERE session_id = ? AND username = ? AND active_from IS NULL',
        (session_id, username),
    ) as cursor:
        perks = [perk for (perk,) in await cursor.fetchall()]
    if perks:
        await db.execute(
            'UPDATE roll_perks SET active_from = ?, active_until = ?'
            ' WHERE session_id = ? AND username = ? AND active_from IS NULL',
            (now, until, session_id, username),
        )
        await db.commit()
    return perks


async def consume_perk(session_id: str, username: str, perk: str) -> None:
    db = await get_db()
    await db.execute(
        'UPDATE roll_perks SET consumed = 1 WHERE session_id = ? AND username = ? AND perk = ?',
        (session_id, username, perk),
    )
    await db.commit()


async def get_pending_perk_users(session_id: str) -> set[str]:
    """Кто в сессии получил бонус, но ещё не появлялся."""
    db = await get_db()
    async with db.execute(
        'SELECT DISTINCT username FROM roll_perks WHERE session_id = ? AND active_from IS NULL',
        (session_id,),
    ) as cursor:
        return {username for (username,) in await cursor.fetchall()}
