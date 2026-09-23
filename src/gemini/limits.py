"""Per-stream limits of the commands that cost a Gemini call: !who, !versus, !summary, !ascii.

Counted in bot_uses under the command's own kind since the stream start, so a restart
resets nothing; offline, where the session is a date, the window is 24 hours.
"""
import logging
from collections.abc import Awaitable, Callable

from src.core.commands import CommandContext
from src.core.content import Content
from src.core.database import count_bot_uses_this_stream, record_bot_use
from src.core.utils import reply

logger = logging.getLogger(__name__)


class PerStreamLimit:
    """One command's limit: one call per viewer at a time, the check, the count.

    The limit is checked before the work and counted after sending, seconds later, and a
    subscriber has neither cooldown nor quota: without one-at-a-time several commands pass
    the same check and overshoot the limit.
    """

    def __init__(self, kind: str, limit_for: Callable[[object], int], error: str) -> None:
        self.kind = kind
        self._limit_for = limit_for     # chatter → limit, 0 – unlimited
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
                limit = self._limit_for(ctx.message.chatter)
                if limit and await count_bot_uses_this_stream(ctx.user, self.kind, ctx.session_id) >= limit:
                    await ctx.refuse()
                    await reply(ctx.message, Content.text(f'{self.kind}_no_left', user=ctx.user, limit=limit))
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
