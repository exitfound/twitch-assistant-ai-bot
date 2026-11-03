"""The game's SQLite queries: rolls, rewards, roll_actions, roll_throws, roll_perks, roll_tiers, roll_chokes.

The tables themselves and their migrations are created by init_db() in
src/core/db/schema.py: the schema lives in one place, which makes it easier to keep
startup on the live database idempotent.
"""
from typing import NamedTuple

from src.core.database import get_db, transaction


async def save_roll(session_id: str, username: str, value: int, *, free_throw: bool) -> None:
    """Save a roll. free_throw=True – the throw counts against the free ones.

    A paid throw, a reroll and a curse leave the counter alone: a victim of sabotage
    must not lose their free throws because of someone else's points.
    rolled_at is written to the millisecond: on equal rolls the latest throw takes the
    title, and two throws often land within one second.
    """
    async with transaction() as db:
        await db.execute(
            'INSERT INTO rolls (session_id, username, roll_value, free_throws, rolled_at)'
            " VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))"
            ' ON CONFLICT(session_id, username) DO UPDATE SET roll_value = excluded.roll_value,'
            ' rolled_at = excluded.rolled_at, free_throws = free_throws + excluded.free_throws',
            (session_id, username, value, int(free_throw)),
        )


async def save_rolls(session_id: str, rolls: list[tuple[str, int]]) -> None:
    """Rolls for many players at once, not free throws: a mass flask. Each is dated a millisecond
    after the one before, so ties among them go to the later in the list, as with real throws;
    the last is dated now, so none lands ahead of a throw that comes right after."""
    last = len(rolls) - 1
    async with transaction() as db:
        await db.executemany(
            'INSERT INTO rolls (session_id, username, roll_value, free_throws, rolled_at)'
            " VALUES (?, ?, ?, 0, strftime('%Y-%m-%d %H:%M:%f', 'now', ?))"
            ' ON CONFLICT(session_id, username) DO UPDATE SET roll_value = excluded.roll_value,'
            ' rolled_at = excluded.rolled_at',
            [(session_id, name, value, f'-{(last - index) / 1000:.3f} seconds')
             for index, (name, value) in enumerate(rolls)],
        )


async def get_rolls(session_id: str) -> dict[str, 'RollRow']:
    """Every player's row in the session, by name."""
    db = await get_db()
    async with db.execute(
        'SELECT username, roll_value, free_throws, curse_ceiling, curse_floor_at, curse_until, curse_power'
        ' FROM rolls WHERE session_id = ?',
        (session_id,),
    ) as cursor:
        return {name: RollRow(*rest) for name, *rest in await cursor.fetchall()}


async def restore_roll(session_id: str, username: str, value: int) -> None:
    """Put back a roll a curse threw over. It loses every tie: dated just before the
    session's earliest roll, so a returned number never takes a title from a real throw."""
    async with transaction() as db:
        await db.execute(
            'UPDATE rolls SET roll_value = ?, rolled_at = ('
            "SELECT strftime('%Y-%m-%d %H:%M:%f', MIN(rolled_at), '-0.001 seconds')"
            ' FROM rolls WHERE session_id = ?) WHERE session_id = ? AND username = ?',
            (value, session_id, session_id, username),
        )


async def save_tier(session_id: str, username: str, tier: str) -> None:
    """The viewer's status in the session, as their latest chat message showed it."""
    async with transaction() as db:
        await db.execute(
            'INSERT INTO roll_tiers (session_id, username, tier) VALUES (?, ?, ?)'
            ' ON CONFLICT(session_id, username) DO UPDATE SET tier = excluded.tier',
            (session_id, username, tier),
        )


async def get_tier(session_id: str, username: str) -> str | None:
    """The viewer's status in the session, None – they have not written in this stream's chat."""
    db = await get_db()
    async with db.execute(
        'SELECT tier FROM roll_tiers WHERE session_id = ? AND username = ?', (session_id, username),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def count_actions(session_id: str, actor: str, action: str) -> int:
    """How many successful purchases of this action the buyer made this session.

    A shield, cleanse or curse a mass flask handed out is journaled with the buyer of the flask
    as its actor and the flask's name: it is not a purchase of theirs and is not counted.
    """
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) FROM roll_actions WHERE session_id = ? AND actor = ? AND action = ? AND status = 'ok'"
        " AND (flask IS NULL OR action = 'flask')",
        (session_id, actor, action),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def reroll_times(session_id: str, actor: str) -> list[int]:
    """When the buyer's successful rerolls of this session were made, in epoch seconds, oldest first."""
    db = await get_db()
    async with db.execute(
        "SELECT CAST(strftime('%s', created_at) AS INTEGER) FROM roll_actions"
        " WHERE session_id = ? AND actor = ? AND action = 'reroll' AND status = 'ok' ORDER BY id",
        (session_id, actor),
    ) as cursor:
        return [seconds for (seconds,) in await cursor.fetchall()]


class RollRow(NamedTuple):
    value: int
    free_throws: int
    curse_ceiling: int | None
    curse_floor_at: float | None
    curse_until: float | None
    curse_power: int | None = None


async def get_roll(session_id: str, username: str) -> RollRow | None:
    """The player's row in the session, or None if they have not rolled."""
    db = await get_db()
    async with db.execute(
        'SELECT roll_value, free_throws, curse_ceiling, curse_floor_at, curse_until, curse_power FROM rolls'
        ' WHERE session_id = ? AND username = ?',
        (session_id, username),
    ) as cursor:
        row = await cursor.fetchone()
    return RollRow(*row) if row else None


async def set_curse(
    session_id: str, username: str, ceiling: int | None, floor_at: float | None,
    until: float | None = None, power: int | None = None,
) -> None:
    """The player's curse; every column is written, so a new curse starts with no doubling."""
    async with transaction() as db:
        await db.execute(
            'UPDATE rolls SET curse_ceiling = ?, curse_floor_at = ?, curse_until = ?, curse_power = ?'
            ' WHERE session_id = ? AND username = ?',
            (ceiling, floor_at, until, power, session_id, username),
        )


async def set_curses(
    session_id: str, curses: list[tuple[str, int | None, float | None, float | None, int | None]],
) -> None:
    """set_curse() for many players at once: (name, ceiling, floor_at, until, power)."""
    async with transaction() as db:
        await db.executemany(
            'UPDATE rolls SET curse_ceiling = ?, curse_floor_at = ?, curse_until = ?, curse_power = ?'
            ' WHERE session_id = ? AND username = ?',
            [(ceiling, floor_at, until, power, session_id, name) for name, ceiling, floor_at, until, power in curses],
        )


async def add_choke(session_id: str, username: str, next_at: float, until: float) -> None:
    """One more choking flask on the player: its first step and its end, by the bot's clock."""
    async with transaction() as db:
        await db.execute(
            'INSERT INTO roll_chokes (session_id, username, next_at, until) VALUES (?, ?, ?, ?)',
            (session_id, username, next_at, until),
        )


async def clear_chokes(session_id: str, usernames: list[str]) -> None:
    """End every choke on these players."""
    async with transaction() as db:
        await db.executemany('DELETE FROM roll_chokes WHERE session_id = ? AND username = ?',
                             [(session_id, name) for name in usernames])


async def get_chokes(session_id: str, username: str, now: float) -> list[float]:
    """When each choke still running on the player ends, by the bot's clock."""
    db = await get_db()
    async with db.execute(
        'SELECT until FROM roll_chokes WHERE session_id = ? AND username = ? AND until > ? ORDER BY until',
        (session_id, username, now),
    ) as cursor:
        return [until for (until,) in await cursor.fetchall()]


async def get_due_chokes(session_id: str, now: float) -> list[tuple[int, str, float, float]]:
    """(id, username, next_at, until) of every choke whose next step is due."""
    db = await get_db()
    async with db.execute(
        'SELECT id, username, next_at, until FROM roll_chokes WHERE session_id = ? AND next_at <= ?'
        ' ORDER BY username, id',
        (session_id, now),
    ) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def move_choke(choke_id: int, next_at: float | None) -> None:
    """A choke's next step; None – its last step was taken, the choke is gone."""
    async with transaction() as db:
        if next_at is None:
            await db.execute('DELETE FROM roll_chokes WHERE id = ?', (choke_id,))
        else:
            await db.execute('UPDATE roll_chokes SET next_at = ? WHERE id = ?', (next_at, choke_id))


async def clear_chokes_outside(session_id: str) -> int:
    """Drop the chokes of every other session: a stream that ended mid-choke leaves its row."""
    async with transaction() as db:
        cursor = await db.execute('DELETE FROM roll_chokes WHERE session_id != ?', (session_id,))
    return cursor.rowcount


async def clear_curses_outside(session_id: str) -> int:
    """Clear the curse columns of every other session. Returns how many rows were cleared."""
    async with transaction() as db:
        cursor = await db.execute(
            'UPDATE rolls SET curse_ceiling = NULL, curse_floor_at = NULL, curse_until = NULL, curse_power = NULL'
            ' WHERE session_id != ? AND curse_ceiling IS NOT NULL',
            (session_id,),
        )
    return cursor.rowcount


async def get_expired_curses(session_id: str, hold: float, now: float) -> list[str]:
    """Who in the session is still marked cursed although the curse has already ended.

    Ended means the ceiling has sat on the floor for `hold` seconds times the curse's
    doubling, or the hard deadline curse_until of the previous stream's loser curse passed.
    """
    db = await get_db()
    async with db.execute(
        'SELECT username FROM rolls WHERE session_id = ? AND curse_ceiling IS NOT NULL'
        ' AND ((curse_floor_at IS NOT NULL AND curse_floor_at + ? * COALESCE(curse_power, 1) <= ?)'
        ' OR (curse_until IS NOT NULL AND curse_until <= ?)) ORDER BY username',
        (session_id, hold, now, now),
    ) as cursor:
        return [username for (username,) in await cursor.fetchall()]


async def get_reward_ids() -> dict[str, str]:
    """{action: Twitch reward id} – which rewards the bot has already created."""
    db = await get_db()
    async with db.execute('SELECT action, reward_id FROM rewards') as cursor:
        rows = await cursor.fetchall()
    return dict(rows)


async def save_reward_id(action: str, reward_id: str) -> None:
    async with transaction() as db:
        await db.execute(
            'INSERT INTO rewards (action, reward_id) VALUES (?, ?)'
            ' ON CONFLICT(action) DO UPDATE SET reward_id = excluded.reward_id',
            (action, reward_id),
        )


async def get_action_status(redemption_id: str) -> str | None:
    """Outcome of an already handled redemption, or None if it has not been seen yet."""
    db = await get_db()
    async with db.execute(
        'SELECT status FROM roll_actions WHERE redemption_id = ?', (redemption_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def save_throw(session_id: str, username: str, thrown_at: float) -> None:
    """A throw the player made by their own hand: a free !roll or a paid extra roll."""
    async with transaction() as db:
        await db.execute(
            'INSERT INTO roll_throws (session_id, username, thrown_at) VALUES (?, ?, ?)',
            (session_id, username, thrown_at),
        )


async def last_throws(session_id: str, username: str, limit: int) -> list[float]:
    """When the player's last `limit` own throws were made, newest first."""
    db = await get_db()
    async with db.execute(
        'SELECT thrown_at FROM roll_throws WHERE session_id = ? AND username = ?'
        ' ORDER BY thrown_at DESC LIMIT ?',
        (session_id, username, limit),
    ) as cursor:
        return [thrown_at for (thrown_at,) in await cursor.fetchall()]


async def last_move_at(session_id: str) -> float | None:
    """When the session's last own throw or redemption was, in epoch seconds. None – none yet."""
    db = await get_db()
    async with db.execute(
        'SELECT MAX(moment) FROM (SELECT MAX(thrown_at) AS moment FROM roll_throws WHERE session_id = ?'
        " UNION ALL SELECT CAST(strftime('%s', MAX(created_at)) AS REAL) FROM roll_actions WHERE session_id = ?)",
        (session_id, session_id),
    ) as cursor:
        (moment,) = await cursor.fetchone()
    return moment


async def count_throws_since(session_id: str, username: str, since: float) -> int:
    """How many own throws the player made from `since` on."""
    db = await get_db()
    async with db.execute(
        'SELECT COUNT(*) FROM roll_throws WHERE session_id = ? AND username = ? AND thrown_at >= ?',
        (session_id, username, since),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def value_before_curse(session_id: str, target: str, flask_seconds: int) -> int | None:
    """The target's roll before the latest bought curse threw over it. None – no such curse, or
    a flask changed the number since: a swap after the curse, or a choke still running after it
    (flask_seconds from its drink). Returning the old roll would undo the flask."""
    db = await get_db()
    async with db.execute(
        "SELECT c.old_value FROM (SELECT * FROM roll_actions WHERE session_id = ? AND action = 'curse'"
        " AND target = ? AND status = 'ok' ORDER BY id DESC LIMIT 1) c"
        " WHERE NOT EXISTS (SELECT 1 FROM roll_actions f WHERE f.session_id = c.session_id"
        " AND f.action = 'flask' AND f.status = 'ok' AND ("
        "(f.flask = 'swap' AND f.id > c.id AND (f.actor = c.target OR f.target = c.target))"
        " OR (f.flask = 'choke' AND f.actor = c.target"
        " AND COALESCE(f.acted_at, CAST(strftime('%s', f.created_at) AS REAL)) + ?"
        " > COALESCE(c.acted_at, CAST(strftime('%s', c.created_at) AS REAL)))))",
        (session_id, target, flask_seconds),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def seconds_since_action(session_id: str, action: str, target: str, status: str, now: float) -> int | None:
    """Seconds from the last such action on the target to `now`. None – there was none.

    `now` is the bot's time.time(), the clock of every other rule of the game. created_at
    is CURRENT_TIMESTAMP in UTC, so strftime('%s') turns it into the same epoch seconds.
    """
    db = await get_db()
    async with db.execute(
        "SELECT CAST(? - strftime('%s', MAX(created_at)) AS INTEGER)"
        ' FROM roll_actions WHERE session_id = ? AND action = ? AND target = ? AND status = ?',
        (now, session_id, action, target, status),
    ) as cursor:
        (seconds,) = await cursor.fetchone()
    return seconds


async def save_action(
    redemption_id: str, session_id: str, action: str, actor: str, user_input: str,
    target: str | None, old_value: int | None, new_value: int | None, status: str,
    flask: str | None = None, acted_at: float | None = None, actor_extra: float | None = None,
) -> None:
    async with transaction() as db:
        await db.execute(
            'INSERT INTO roll_actions (redemption_id, session_id, action, actor, user_input, target, old_value,'
            ' new_value, status, flask, acted_at, actor_extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (redemption_id, session_id, action, actor, user_input, target, old_value, new_value, status, flask,
             acted_at, actor_extra),
        )


async def save_grants(
    session_id: str, flask: str, actor: str, acted_at: float, grants: list[tuple[str, str, str]],
) -> None:
    """What a mass flask gave each player, journaled at once: (redemption_id, action, player)."""
    async with transaction() as db:
        await db.executemany(
            'INSERT INTO roll_actions (redemption_id, session_id, action, actor, user_input, target, status,'
            " flask, acted_at) VALUES (?, ?, ?, ?, '', ?, 'ok', ?, ?)",
            [(redemption_id, session_id, action, actor, name, flask, acted_at)
             for redemption_id, action, name in grants],
        )


async def get_held(session_id: str, now: float, flask_seconds: int) -> set[str]:
    """Who a swap holds now, in one query: its buyer (flask_seconds plus actor_extra) and its partner
    (flask_seconds), unless БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА reached them after the swap – game._holds()'s
    rule for everyone at once."""
    swap = ("SELECT {who} AS name, COALESCE(acted_at, CAST(strftime('%s', created_at) AS REAL)) AS moment,"
            " {extra} AS extra FROM roll_actions WHERE session_id = ? AND action = 'flask'"
            " AND flask = 'swap' AND status = 'ok'")
    db = await get_db()
    async with db.execute(
        f"SELECT name FROM ({swap.format(who='actor', extra='COALESCE(actor_extra, 0)')}"
        f" UNION ALL {swap.format(who='target', extra='0')}) s WHERE s.moment + ? + s.extra > ?"
        " AND NOT EXISTS (SELECT 1 FROM roll_actions b WHERE b.session_id = ? AND b.action != 'flask'"
        " AND b.flask = 'blessing' AND b.status IN ('ok', 'revoked') AND b.target = s.name"
        ' AND b.acted_at >= s.moment)',
        (session_id, session_id, flask_seconds, now, session_id),
    ) as cursor:
        return {name for (name,) in await cursor.fetchall() if name}


async def flask_drinks(session_id: str, flask: str, username: str) -> list[float]:
    """When the player drank each successful flask of this kind this session, by the bot's clock,
    oldest first."""
    db = await get_db()
    async with db.execute(
        "SELECT COALESCE(acted_at, CAST(strftime('%s', created_at) AS REAL)) AS moment FROM roll_actions"
        " WHERE session_id = ? AND action = 'flask' AND flask = ? AND status = 'ok' AND actor = ? ORDER BY moment",
        (session_id, flask, username),
    ) as cursor:
        return [moment for (moment,) in await cursor.fetchall()]


async def swap_holds(session_id: str, username: str, flask_seconds: int) -> list[tuple[float, float]]:
    """(drunk_at, ends_at) of every successful swap the player was in this session, as its buyer or its
    partner: a buyer is held flask_seconds plus the row's actor_extra, a partner flask_seconds."""
    db = await get_db()
    async with db.execute(
        "SELECT COALESCE(acted_at, CAST(strftime('%s', created_at) AS REAL)) AS moment,"
        ' CASE WHEN actor = ? THEN COALESCE(actor_extra, 0) ELSE 0 END FROM roll_actions'
        " WHERE session_id = ? AND action = 'flask' AND flask = 'swap' AND status = 'ok'"
        ' AND (actor = ? OR target = ?) ORDER BY moment',
        (username, session_id, username, username),
    ) as cursor:
        return [(moment, moment + flask_seconds + extra) for moment, extra in await cursor.fetchall()]


async def flask_granted_at(
    session_id: str, flask: str, username: str, statuses: tuple[str, ...] = ('ok',),
) -> float | None:
    """When a mass flask of this kind last reached the player, by the bot's clock, counting its
    rows of these statuses. None – none did."""
    db = await get_db()
    marks = ', '.join('?' * len(statuses))
    async with db.execute(
        "SELECT MAX(acted_at) FROM roll_actions WHERE session_id = ? AND action != 'flask' AND flask = ?"
        f' AND status IN ({marks}) AND target = ?',
        (session_id, flask, *statuses, username),
    ) as cursor:
        (moment,) = await cursor.fetchone()
    return moment


async def flask_last_at(session_id: str, flask: str) -> float | None:
    """When anyone last drank a flask of this kind successfully, by the bot's clock. None – nobody did."""
    db = await get_db()
    async with db.execute(
        "SELECT MAX(acted_at) FROM roll_actions WHERE session_id = ? AND action = 'flask' AND flask = ?"
        " AND status = 'ok'",
        (session_id, flask),
    ) as cursor:
        (moment,) = await cursor.fetchone()
    return moment


async def revoke_grants(session_id: str, flask: str, status: str) -> None:
    """Take back what a mass flask handed out this session: its rows stop counting as ok."""
    async with transaction() as db:
        await db.execute(
            "UPDATE roll_actions SET status = ? WHERE session_id = ? AND action != 'flask' AND flask = ?"
            " AND status = 'ok'",
            (status, session_id, flask),
        )


async def get_present(session_id: str) -> set[str]:
    """Everyone the stream knows: who has written in its chat (roll_tiers) or has a roll."""
    db = await get_db()
    async with db.execute(
        'SELECT username FROM roll_tiers WHERE session_id = ? UNION SELECT username FROM rolls WHERE session_id = ?',
        (session_id, session_id),
    ) as cursor:
        return {name for (name,) in await cursor.fetchall()}


async def get_shielded(session_id: str, now: float, shield_seconds: int) -> set[str]:
    """Who a swap may not draw for a shield, in one query: a shield younger than shield_seconds
    (bought or handed out by БЛАГОСЛОВЕНИЕ СВЕТЛОЯРА), a Благодать running or not started yet.
    The window is game.py's, the same its own check uses one player at a time."""
    db = await get_db()
    async with db.execute(
        "SELECT target FROM roll_actions WHERE session_id = ? AND action = 'shield' AND status = 'ok'"
        " AND ? - CAST(strftime('%s', created_at) AS INTEGER) < ?"
        " UNION SELECT username FROM roll_perks WHERE session_id = ? AND perk = 'shield' AND consumed = 0"
        ' AND (active_until IS NULL OR active_until > ?)',
        (session_id, now, shield_seconds, session_id, now),
    ) as cursor:
        return {name for (name,) in await cursor.fetchall() if name}


async def get_players(session_id: str) -> list[tuple[str, int]]:
    """(username, roll_value) of everyone who has a roll in the session."""
    db = await get_db()
    async with db.execute(
        'SELECT username, roll_value FROM rolls WHERE session_id = ? ORDER BY id', (session_id,),
    ) as cursor:
        return [(username, value) for username, value in await cursor.fetchall()]


async def set_roll_value(session_id: str, username: str, value: int) -> None:
    """Put a number into a player's roll and nothing else: the throw time, the free throws
    and a curse stay, so a tie is still settled by who threw last."""
    async with transaction() as db:
        await db.execute(
            'UPDATE rolls SET roll_value = ? WHERE session_id = ? AND username = ?',
            (value, session_id, username),
        )


async def get_session_titles(
    session_id: str, without: str | None = None,
) -> tuple[tuple[str, int] | None, tuple[str, int] | None]:
    """The session's two titles, (champion, loser): the one rule the game and the bot's memory share.

    The loser is the lowest roll – matching it makes you the loser – and the champion the highest
    of the rest, so nobody holds both, even when every roll is equal. A lone player is the
    champion by default, and there is no loser. without – a player left out, as if they never threw.
    """
    skip = (without,) if without else ()
    loser = await get_session_loser(session_id, exclude=skip)
    champion = await get_session_champion(session_id, exclude=skip + ((loser[0],) if loser else ()))
    if champion is None:
        return loser, None
    return champion, loser


async def get_month_sessions(month: str) -> list[str]:
    """Sessions of a month (YYYY-MM) that have rolls, oldest first.

    A session id starts with the stream's start date, so a stream past midnight on the
    last day stays in its month.
    """
    db = await get_db()
    async with db.execute(
        'SELECT DISTINCT session_id FROM rolls WHERE substr(session_id, 1, 7) = ? ORDER BY session_id',
        (month,),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


def _not_in(exclude: tuple[str, ...]) -> str:
    """The SQL clause leaving these players out; empty when there are none."""
    return f" AND username NOT IN ({', '.join('?' * len(exclude))})" if exclude else ''


async def get_session_loser(session_id: str, exclude: tuple[str, ...] = ()) -> tuple[str, int] | None:
    """Returns (username, roll_value) of the session залупа (minimum roll), leaving out `exclude`.

    On equal minimums the latest throw takes the title: matching the loser makes you
    the loser. Rows written before rolled_at had milliseconds compare correctly with
    newer ones; id only settles what is left, so the answer never changes between queries.
    """
    db = await get_db()
    async with db.execute(
        'SELECT username, roll_value FROM rolls WHERE session_id = ?' + _not_in(exclude)
        + ' ORDER BY roll_value ASC, rolled_at DESC, id DESC LIMIT 1',
        (session_id, *exclude),
    ) as cursor:
        row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def get_session_champion(session_id: str, exclude: tuple[str, ...] = ()) -> tuple[str, int] | None:
    """(username, roll_value) of the session китежанин (champion) – the highest roll, leaving out `exclude`.

    Mirror of get_session_loser(): on equal maximums the latest throw takes the title.
    """
    db = await get_db()
    async with db.execute(
        'SELECT username, roll_value FROM rolls WHERE session_id = ?' + _not_in(exclude)
        + ' ORDER BY roll_value DESC, rolled_at DESC, id DESC LIMIT 1',
        (session_id, *exclude),
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
    async with transaction() as db:
        cursor = await db.execute(
            'INSERT OR IGNORE INTO roll_perks (session_id, username, perk, from_session) VALUES (?, ?, ?, ?)',
            (session_id, username, perk, from_session),
        )
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
    """Start the countdown of the player's not yet started perks. Returns which were started.

    A perk consumed before it started (the loser curse lifted by a cleanse) stays as it
    is: started, it would be announced as laid.
    """
    async with transaction() as db:
        async with db.execute(
            'SELECT perk FROM roll_perks'
            ' WHERE session_id = ? AND username = ? AND active_from IS NULL AND consumed = 0',
            (session_id, username),
        ) as cursor:
            perks = [perk for (perk,) in await cursor.fetchall()]
        if perks:
            await db.execute(
                'UPDATE roll_perks SET active_from = ?, active_until = ?'
                ' WHERE session_id = ? AND username = ? AND active_from IS NULL AND consumed = 0',
                (now, until, session_id, username),
            )
    return perks


async def consume_perk(session_id: str, username: str, perk: str) -> None:
    async with transaction() as db:
        await db.execute(
            'UPDATE roll_perks SET consumed = 1 WHERE session_id = ? AND username = ? AND perk = ?',
            (session_id, username, perk),
        )


async def get_waiting_curses(session_id: str, now: float) -> set[str]:
    """Whose previous stream's loser curse still waits for its first throw."""
    db = await get_db()
    async with db.execute(
        "SELECT username FROM roll_perks WHERE session_id = ? AND perk = 'curse' AND consumed = 0"
        ' AND (active_until IS NULL OR active_until > ?)',
        (session_id, now),
    ) as cursor:
        return {name for (name,) in await cursor.fetchall()}


async def consume_perks(session_id: str, usernames: list[str], perk: str) -> None:
    """consume_perk() for many players at once."""
    async with transaction() as db:
        await db.executemany(
            'UPDATE roll_perks SET consumed = 1 WHERE session_id = ? AND username = ? AND perk = ?',
            [(session_id, name, perk) for name in usernames],
        )


async def get_pending_perk_users(session_id: str) -> set[str]:
    """Who got a perk this session but has not shown up yet."""
    db = await get_db()
    async with db.execute(
        'SELECT DISTINCT username FROM roll_perks'
        ' WHERE session_id = ? AND active_from IS NULL AND consumed = 0',
        (session_id,),
    ) as cursor:
        return {username for (username,) in await cursor.fetchall()}
