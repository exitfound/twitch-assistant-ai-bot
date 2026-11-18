"""!schedule – the nearest stream from the channel's schedule on Twitch.

The schedule is kept on Twitch itself, so the bot reads it there instead of a copy in
CONTENT.md that would go stale. A channel command: listed in !channel, one shared pause.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import twitchio
from twitchio.http import Route

from src.core.config import Clock, Twitch
from src.core.content import Content
from src.twitch.core.commands import CommandContext
from src.twitch.core.replies import reply

logger = logging.getLogger(__name__)

TRIGGER = '!schedule'
# Slots in one answer: the nearest stream only, the rest is on the schedule page
SLOTS = 1
# Segments asked for: cancelled and started ones are skipped, so a few more than SLOTS
FETCH = 10
# A schedule changes rarely: one Helix request per this many seconds at most
CACHE_SECONDS = 600
FETCH_TIMEOUT = 10

# (monotonic time of the fetch, the schedule as Helix gave it)
_cache: tuple[float, dict] | None = None


@dataclass(frozen=True)
class Slot:
    start: datetime
    title: str
    category: str


@dataclass(frozen=True)
class Plan:
    slots: list[Slot]
    # The end of a vacation that is on right now
    vacation_until: datetime | None


async def fetch(http, channel_id: str) -> dict | None:
    """The channel's schedule as Helix gives it, {} if none is set; None – Twitch did not say.

    A raw request: twitchio's own iterator fails on a schedule with no segments.
    """
    route = Route('GET', 'schedule', params={'broadcaster_id': channel_id, 'first': FETCH})
    try:
        async with asyncio.timeout(FETCH_TIMEOUT):
            data = await http.request_json(route)
        return data.get('data') or {}
    except twitchio.HTTPException as e:
        # Helix answers 404 to a channel with no segments in its schedule
        if e.status == 404:
            return {}
        logger.warning('Расписание канала не получено: %r', e)
    except Exception as e:
        logger.warning('Расписание канала не получено: %r', e)
    return None


def _time(value: str | None) -> datetime | None:
    """A Helix timestamp; one without an offset is UTC, so it compares with an aware now."""
    try:
        moment = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment and moment.tzinfo is None else moment


def parse(data: dict, now: datetime) -> Plan:
    """Upcoming slots that are not cancelled nor inside a vacation, nearest first.

    The vacation itself is reported only while it lasts.
    """
    vacation = data.get('vacation') or {}
    begin, end = _time(vacation.get('start_time')), _time(vacation.get('end_time'))
    slots = []
    for segment in data.get('segments') or []:
        start = _time(segment.get('start_time'))
        if start is None or start <= now or segment.get('canceled_until'):
            continue
        # Recurring slots stay in the schedule over a vacation: none of them is a stream
        if begin and end and begin <= start < end:
            continue
        category = (segment.get('category') or {}).get('name') or ''
        slots.append(Slot(start, segment.get('title') or '', category))
    slots.sort(key=lambda slot: slot.start)
    until = end if begin and end and begin <= now < end else None
    return Plan(slots[:SLOTS], until)


def _slot(slot: Slot) -> str:
    start = slot.start.astimezone(Clock.ZONE)
    weekdays = Content.items('weekdays')
    weekday = weekdays[start.weekday()] if len(weekdays) == 7 else ''
    text = Content.text('schedule_slot', weekday=weekday, date=start.strftime('%d.%m'),
                        time=start.strftime('%H:%M'), title=slot.title, category=slot.category)
    # An empty weekday or title must not leave a double space behind
    return ' '.join(text.split())


def render(plan: Plan | None, user: str) -> str:
    url = f'https://www.twitch.tv/{(Twitch.CHANNEL or "").lower()}/schedule'
    if plan is None:
        return Content.text('schedule_failed', user=user, url=url)
    if plan.vacation_until:
        until = plan.vacation_until.astimezone(Clock.ZONE).strftime('%d.%m')
        return Content.text('schedule_vacation', user=user, until=until, url=url)
    if not plan.slots:
        return Content.text('schedule_empty', user=user, url=url)
    slots = ', '.join(_slot(slot) for slot in plan.slots)
    return Content.text('schedule', user=user, slots=slots, url=url)


async def _schedule(ctx: CommandContext) -> dict | None:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    data = await ctx.bot.fetch_schedule()
    # A failure is not kept: the next call asks Twitch again
    if data is not None:
        _cache = (now, data)
    return data


async def handle_schedule(ctx: CommandContext) -> None:
    data = await _schedule(ctx)
    plan = parse(data, datetime.now(Clock.ZONE)) if data is not None else None
    await reply(ctx.message, render(plan, ctx.user))
