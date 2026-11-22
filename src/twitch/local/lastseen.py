"""!lastseen <nick> – when a viewer last wrote in the channel's chat.

The whole stored history counts, commands do not: they are not chat. Listed by !channel,
but with a personal cooldown – the nick differs from call to call, so one shared pause
would leave the second viewer unanswered.
"""
from datetime import datetime, timedelta

from src.core.config import Clock
from src.core.content import Content
from src.core.database import get_last_message_time
from src.twitch.core.commands import CommandContext
from src.twitch.core.replies import reply
from src.core.utils import parse_nick

TRIGGER = '!lastseen'


def when(ts: float, now: datetime) -> str:
    """«сегодня в 21:40», «вчера в 21:40» or «18.11.2026 в 21:40», in the bot's zone."""
    moment = datetime.fromtimestamp(ts, Clock.ZONE)
    time = moment.strftime('%H:%M')
    today = now.astimezone(Clock.ZONE).date()
    if moment.date() == today:
        return Content.text('lastseen_today', time=time)
    if moment.date() == today - timedelta(days=1):
        return Content.text('lastseen_yesterday', time=time)
    return Content.text('lastseen_date', date=moment.strftime('%d.%m.%Y'), time=time)


async def handle_lastseen(ctx: CommandContext) -> None:
    target = parse_nick(ctx.args)
    if target is None:
        # Malformed input: nothing was answered, so there is nothing to wait for
        ctx.clear_cooldown()
        await reply(ctx.message, Content.text('lastseen_usage', user=ctx.user, command=TRIGGER))
        return
    ts = await get_last_message_time(target)
    if ts is None:
        await reply(ctx.message, Content.text('lastseen_unknown', user=ctx.user, target=target))
        return
    await reply(ctx.message, Content.text('lastseen', user=ctx.user, target=target,
                                          when=when(ts, datetime.now(Clock.ZONE))))
