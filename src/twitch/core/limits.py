"""Per-stream limits of commands: !ask, !who, !versus, !summary, !ascii, !clip.

One ladder for all of them, counted in bot_uses under each command's own kind since the
stream start, so a restart resets nothing; offline, where the session is a date, the
window is 24 hours.
"""
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from src.twitch.core.commands import CommandContext
from src.core.config import PerStream
from src.core.content import Content
from src.core.database import count_bot_uses_this_stream, record_bot_use
from src.twitch.core.replies import reply
from src.twitch.core.viewer import Tier, by_tier, sub_hint, tier_of

if TYPE_CHECKING:
    import twitchio

    from src.twitch.core.port import BotPort

logger = logging.getLogger(__name__)

# Cooldown scope of refusals by a limit: the hourly quota, the channel ceiling, the
# per-stream limit. The fact does not change between repeats, and without this brake
# every message of a viewer past their limit would get a refusal
DENY_SCOPE = 'deny'
DENY_REPEAT_SECONDS = 30


async def deny(bot: 'BotPort', message: 'twitchio.ChatMessage', user: str, text: str) -> None:
    """Refuse, but at most once per DENY_REPEAT_SECONDS per viewer."""
    if bot.cooldown_remaining(user, DENY_SCOPE):
        return
    bot.set_cooldown(user, DENY_REPEAT_SECONDS, DENY_SCOPE)
    await reply(message, text)


def limit_for(chatter) -> int:
    """A viewer's per-stream limit, 0 – unlimited."""
    return by_tier(tier_of(chatter), broadcaster=0, sub=PerStream.SUB,
                   vip=PerStream.VIP, regular=PerStream.FOLLOWER)


def no_left_text(ctx: CommandContext, command: str, limit: int) -> str:
    """The refusal once the limit is used up. A follower has had a trial and is offered
    a subscription; a VIP gets the usual refusal with the subscription hint."""
    chatter = ctx.message.chatter
    if tier_of(chatter) == Tier.REGULAR:
        return Content.text('per_stream_trial_used', user=ctx.user, command=command, limit=limit)
    text = Content.text('per_stream_no_left', user=ctx.user, command=command, limit=limit)
    return text + sub_hint(chatter)


class PerStreamLimit:
    """One command's limit: one call per viewer at a time, the check, the count.

    The limit is checked before the work and counted after sending, seconds later, and a
    subscriber has neither cooldown nor quota: without one-at-a-time several commands pass
    the same check and overshoot the limit.
    """

    def __init__(self, kind: str, error: str) -> None:
        self.kind = kind                # the command without «!», and its kind in bot_uses
        self._error = error             # text key for an unexpected failure
        self.busy: set[str] = set()

    async def run(self, ctx: CommandContext, serve: Callable[[], Awaitable[bool]]) -> None:
        """serve() does the work and returns whether it reached chat: only that is counted,
        so a refusal costs nothing."""
        if ctx.user in self.busy:
            # The first command is still working and will answer itself: drop the repeat
            # silently and refund its quota
            await ctx.refuse()
            return
        # Taken before the first await, or two commands at once both pass the check
        self.busy.add(ctx.user)
        try:
            try:
                limit = limit_for(ctx.message.chatter)
                if limit and await count_bot_uses_this_stream(ctx.user, self.kind, ctx.session_id) >= limit:
                    await ctx.refuse()
                    await deny(ctx.bot, ctx.message, ctx.user, no_left_text(ctx, f'!{self.kind}', limit))
                    return
                sent = await serve()
            except Exception:
                # The viewer paid a quota slot and must hear something rather than nothing
                logger.exception('!%s: ошибка для %s', self.kind, ctx.user)
                await reply(ctx.message, Content.text(self._error, user=ctx.user))
                return
            if sent:
                # The answer is already in chat: an error here is logged, not answered
                try:
                    await record_bot_use(ctx.user, self.kind)
                except Exception:
                    logger.exception('!%s для %s отправлен, но не учтён в лимите', self.kind, ctx.user)
        finally:
            self.busy.discard(ctx.user)
