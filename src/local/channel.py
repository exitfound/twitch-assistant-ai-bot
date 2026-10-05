"""The channel's own commands: `!tg`, `!donate` – a fixed reply from CONTENT.md.

Nothing about the bot itself: links and the like, the way other chat bots serve them.
The list is the `channel` section of CONTENT.md, re-read live, so a command added there
works without a restart.
"""
import functools

from src.core.commands import CommandContext, CommandEntry, Kind
from src.core.content import Content
from src.core.utils import reply, safe_format


def resolve(prompt: str) -> CommandEntry | None:
    """The channel command the message starts with, or None."""
    for trigger in Content.channel_commands():
        # A prefix entry: «!tg пж» answers too, «!tgx» does not
        entry = CommandEntry(trigger=trigger, handler=functools.partial(_answer, trigger=trigger),
                             prefix=True, kind=Kind.LOCAL, public=True)
        if entry.match(prompt):
            return entry
    return None


async def _answer(ctx: CommandContext, trigger: str) -> None:
    text = Content.channel_commands().get(trigger)
    if not text:
        # Removed from CONTENT.md between the lookup and the answer
        ctx.clear_cooldown()
        return
    await reply(ctx.message, safe_format(text, user=ctx.user))
