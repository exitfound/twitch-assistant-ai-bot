"""The game's SQLite queries: rolls, rewards, roll_actions, roll_perks.

The tables themselves and their migrations are created by init_db() in
src/core/database.py: the schema lives in one place, which makes it easier to keep
startup on the live database idempotent.
"""
from typing import NamedTuple

from src.core.database import get_db


async def save_roll(
    session_id: str, username: str, value: int, *, free_throw: bool, limit: int | None = None,
) -> None:
    """Save a roll. free_throw=True – the throw counts against the free ones.

    A paid throw, a reroll and a curse leave the counter alone: a victim of sabotage
    must not lose their free throws because of someone else's points.

    limit – how many free throws the player gets by their status. It is known only
    when the person rolls from chat: a reward redemption event carries no badges,
    so the limit is stored in the row.
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
    free_limit: int | None      # status-based limit, stored on a throw from chat


async def get_roll(session_id: str, username: str) -> RollRow | None:
    """The player's row in the session, or None if they have not rolled."""
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
    """Who in the session is still marked cursed although the curse has already ended.

    Ended means the ceiling reached the floor before floor_before, or the hard deadline
    curse_until of the previous stream's loser curse passed.
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
    """{action: Twitch reward id} – which rewards the bot has already created."""
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
    """Outcome of an already handled redemption, or None if it has not been seen yet."""
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
    """Seconds since the last such action on the target. None – there was none.

    SQLite itself computes the time: created_at is written by its CURRENT_TIMESTAMP in
    UTC, and comparing it with Python's clock would mean keeping track of time zones.
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

    On equal minimums whoever rolled earlier wins. rolled_at has one-second
    precision, so the final tie-break is by id: otherwise two equal rolls in
    the same second would give a different answer from query to query.
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
    """(username, roll_value) of the session китежанин (champion) – the highest roll.

    Mirror of get_session_loser(): on equal maximums the title goes to whoever
    threw earlier, the final tie-break is by id.
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
    """The latest session with rolls, other than this one, whose throws ended before `before`.

    Needed when the previous stream is not in the streams table and the results have to
    come from a date session instead. Throws made inside the current stream do not count.
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


# --- perks from the previous stream ----------------------------------------------

class PerkRow(NamedTuple):
    active_until: float | None
    consumed: bool


async def add_perk(session_id: str, username: str, perk: str, from_session: str) -> bool:
    """Grant a perk. False – this perk was already granted in this session."""
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
    """Start the countdown of the player's not yet started perks. Returns which were started."""
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
    """Who got a perk this session but has not shown up yet."""
    db = await get_db()
    async with db.execute(
        'SELECT DISTINCT username FROM roll_perks WHERE session_id = ? AND active_from IS NULL',
        (session_id,),
    ) as cursor:
        return {username for (username,) in await cursor.fetchall()}
