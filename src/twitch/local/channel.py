"""The channel's own commands: `!tg`, `!donate` – a fixed reply from CONTENT.md.

Nothing about the bot itself: links and the like, the way other chat bots serve them.
The list is the `channel` section of CONTENT.md, re-read live, so a command added there
works without a restart and shows up in !channel.
"""
import functools
import logging
from collections.abc import Callable

from src.twitch.core.commands import CommandContext, CommandEntry, Kind
from src.core.content import Content
from src.core.utils import safe_format
from src.twitch.core.replies import reply
from src.twitch.local import help_announce

logger = logging.getLogger(__name__)

# Twitch refuses a chat message longer than this, and the bot then stays silent
MESSAGE_MAX = 500
# The longest Twitch login: what {user} can add to a reply
NICK_MAX = 25


class ChannelCommands:
    """The registry's fallback: the section is checked again whenever it changes."""

    def __init__(self, is_taken: Callable[[str], bool]) -> None:
        # Whether a bot command answers this trigger first
        self._is_taken = is_taken
        self._checked: dict[str, str] | None = None

    def resolve(self, prompt: str) -> CommandEntry | None:
        """The channel command the message starts with, or None."""
        for trigger in self.check():
            # A prefix entry: «!tg пж» answers too, «!tgx» does not
            entry = CommandEntry(trigger=trigger, handler=functools.partial(_answer, trigger=trigger),
                                 prefix=True, kind=Kind.LOCAL, public=True)
            if entry.match(prompt):
                return entry
        return None

    def check(self) -> dict[str, str]:
        """The current commands; an edit is logged once, at the first message after it."""
        commands = Content.channel_commands()
        if commands != self._checked:
            self._checked = commands
            _report(commands, self._is_taken)
        return commands


def _report(commands: dict[str, str], is_taken: Callable[[str], bool]) -> None:
    shadowed = [trigger for trigger in commands if is_taken(trigger)]
    if shadowed:
        logger.error('CONTENT.md: команды канала %s совпадают с командами бота и не сработают',
                     ', '.join(shadowed))
    for trigger, text in commands.items():
        if len(safe_format(text, user='x' * NICK_MAX)) > MESSAGE_MAX:
            logger.warning('CONTENT.md: ответ %s длиннее %d символов – Twitch его не пропустит',
                           trigger, MESSAGE_MAX)
    if commands and len(_listing('x' * NICK_MAX, commands)) > MESSAGE_MAX:
        logger.warning('CONTENT.md: список !channel длиннее %d символов – Twitch его не пропустит',
                       MESSAGE_MAX)


def _listing(user: str, commands: dict[str, str]) -> str:
    return Content.text('help_channel', user=user, commands=' | '.join(commands))


async def handle_help_channel(ctx: CommandContext) -> None:
    """!channel – the list of the channel's own commands, without their replies."""
    help_announce.note_help_shown()
    commands = Content.channel_commands()
    if not commands:
        await reply(ctx.message, Content.text('help_channel_empty', user=ctx.user))
        return
    await reply(ctx.message, _listing(ctx.user, commands))


async def _answer(ctx: CommandContext, trigger: str) -> None:
    text = Content.channel_commands().get(trigger)
    # Removed from CONTENT.md between the lookup and the answer: nothing to say
    if text:
        await reply(ctx.message, safe_format(text, user=ctx.user))
