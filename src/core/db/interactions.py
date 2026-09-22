"""bot_interactions: what the bot answered, by tag and by viewer."""
from src.core.db.connection import get_db, transaction


async def save_bot_interaction(session_id: str, username: str, user_message: str, bot_response: str) -> None:
    async with transaction() as db:
        await db.execute(
            'INSERT INTO bot_interactions (session_id, username, user_message, bot_response) VALUES (?, ?, ?, ?)',
            (session_id, username, user_message, bot_response),
        )


async def get_last_tagged_interaction(
    username: str, tag: str, window_minutes: int,
) -> tuple[str, str] | None:
    """The viewer's last exchange with the bot on command tag within window_minutes.

    Returns (question without the tag, answer) or None. Needed by !ask for follow-ups:
    the model cannot make sense of «а подробнее?» without the previous question.
    """
    prefix = f'{tag} '
    db = await get_db()
    async with db.execute(
        "SELECT user_message, bot_response FROM bot_interactions"
        " WHERE username = ? AND substr(user_message, 1, ?) = ?"
        " AND created_at > datetime('now', ?) ORDER BY id DESC LIMIT 1",
        (username, len(prefix), prefix, f'-{window_minutes} minutes'),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row[0][len(prefix):], row[1]


async def get_user_interactions(username: str, limit: int = 10) -> list[tuple[str, str]]:
    """The viewer's own exchanges with the bot – free text only, no commands.

    The pattern is '[%]%' because a tag is a prefix, not the whole line. A pattern that
    matched only rows ending in ']' would let «[ask] вопрос» and «[who] ник» through, and
    !who would feed the model its own past answers.
    """
    db = await get_db()
    async with db.execute(
        'SELECT user_message, bot_response FROM bot_interactions '
        "WHERE username = ? AND user_message NOT LIKE '[%]%' ORDER BY id DESC LIMIT ?",
        (username, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return list(reversed(rows))


async def get_tagged_answers(tags: list[str], limit: int) -> list[str]:
    """The bot's last answers saved under any of these tags, whoever asked – «[who] nick»
    finds what the bot already said about the nick."""
    db = await get_db()
    marks = ','.join('?' * len(tags))
    async with db.execute(
        f'SELECT bot_response FROM bot_interactions WHERE user_message IN ({marks})'
        ' ORDER BY id DESC LIMIT ?',
        (*tags, limit),
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]
