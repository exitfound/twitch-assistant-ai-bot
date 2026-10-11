"""The game on the stream: events for the OBS page, which shows them as cards and titles.

GET /roll/        – the page (overlay.html), the Browser Source in OBS
GET /roll/events  – text/event-stream, event `roll`: the state on connect, then every event
GET /roll/state   – the state once, for a quick look from a browser

The callers that tell chat about the game tell the page too; publish() never raises and
never waits, so the game and chat are unaffected by the page. Nicks leave the port here,
unlike the mascot feed. Every payload is a dict with `type`; the page's renderer knows them.
"""
import logging
import time
from pathlib import Path

from aiohttp import web

from src.core.config import Rewards, Roll
from src.twitch.core.sse import CORS, Feed
from src.twitch.local.roll import game

logger = logging.getLogger(__name__)

PAGE = Path(__file__).with_name('overlay.html')

# Throws: what the page shows as a number with «out of»
_THROWN = {game.Action.EXTRA: 'extra', game.Action.REROLL: 'reroll', game.Action.CURSE: 'curse'}
# A reroll that hit a protection: the points went back, the page shows it bounce off
_BLOCKED = {game.Status.SHIELDED: 'shield', game.Status.PERK_SHIELDED: 'bless'}

_state: dict = {'type': 'state', 'live': False, 'titles': {'best': None, 'worst': None}}
# The last game event on the monotonic clock: the page shows the titles for a while after one
_active_at: float | None = None
# Bumped by every event that carries titles: refresh() reads the DB across an await and must
# not overwrite titles a throw published meanwhile
_titles_version = 0


def _snapshot() -> dict:
    """The state a page starts from; `quiet` – seconds since the last game event, None – none yet."""
    quiet = None if _active_at is None else round(time.monotonic() - _active_at)
    return {**_state, 'min': Roll.MIN, 'max': Roll.MAX, 'quiet': quiet}


feed = Feed('roll', _snapshot)


def _nick_value(pair: tuple[str, int] | None) -> dict | None:
    return {'nick': pair[0], 'value': pair[1]} if pair else None


def _titles(loser: tuple[str, int] | None, champion: tuple[str, int] | None) -> dict:
    return {'best': _nick_value(champion), 'worst': _nick_value(loser)}


def _with_titles(event: dict, outcome: game.Outcome) -> dict:
    """An event that changed a roll carries the titles after it."""
    if outcome.loser is not None:
        event['titles'] = _titles(outcome.loser, outcome.champion)
    return event


def thrown(kind: str, who: str, actor: str | None, outcome: game.Outcome) -> dict:
    """A throw on a player: !roll, an extra roll, a reroll or a curse."""
    return _with_titles({
        'type': kind, 'who': who, 'actor': actor, 'value': outcome.value, 'old': outcome.old_value,
        'min': Roll.MIN, 'max': Roll.MAX, 'ceiling': outcome.ceiling, 'next': outcome.next_ceiling,
        'hold': outcome.curse_minutes_left, 'floor': Rewards.CURSE_FLOOR,
    }, outcome)


def redeemed(action: str, actor: str, outcome: game.Outcome) -> dict | None:
    """A reward's outcome for the page. None – a refusal the stream does not need to see."""
    if outcome.status in _BLOCKED:
        return {'type': 'blocked', 'who': outcome.target, 'actor': actor,
                'by': _BLOCKED[outcome.status], 'minutes': outcome.protect_minutes_left}
    if not outcome.ok:
        return None
    if action in _THROWN:
        return thrown(_THROWN[action], outcome.target, actor, outcome)
    if action == game.Action.SHIELD:
        return {'type': 'shield', 'who': actor, 'minutes': Rewards.SHIELD_MINUTES}
    if action == game.Action.CLEANSE:
        # value is set only when the cleanse returned the roll the curse threw over
        return _with_titles({'type': 'cleanse', 'who': outcome.target, 'actor': actor, 'value': outcome.value,
                             'old': outcome.old_value, 'max': Roll.MAX, 'minutes': outcome.protect_minutes_left},
                            outcome)
    return None


def lifted(user: str) -> dict:
    return {'type': 'lift', 'who': user, 'max': Roll.MAX}


def perks(champion: str | None, loser: str | None) -> dict:
    """The previous stream's results, at the start of this one."""
    return {'type': 'perks', 'best': champion, 'worst': loser,
            'minutes': Roll.PERK_MINUTES, 'ceiling': Rewards.CURSE_CEILING}


def perk_on(user: str, perk: str) -> dict:
    """A perk's countdown started: the player showed up in chat."""
    return {'type': 'perk', 'who': user, 'perk': 'bless' if perk == game.Perk.SHIELD else 'sglaz',
            'minutes': Roll.PERK_MINUTES, 'ceiling': Rewards.CURSE_CEILING}


def wake() -> dict:
    """A game command with nothing to show as a card (!rollstat, a refusal): the titles come back."""
    return {'type': 'wake'}


def publish(event: dict | None) -> None:
    """Hand an event to every open page. Titles it carries become the state of the next page."""
    global _active_at, _titles_version
    if event is None or not Roll.OVERLAY_ENABLED:
        return
    try:
        _active_at = time.monotonic()
        if 'titles' in event:
            _state['titles'] = event['titles']
            _titles_version += 1
        feed.publish(event)
    except Exception:
        logger.exception('Событие игры для оверлея не отправлено')


async def refresh(session_id: str, live: bool) -> None:
    """Titles from the database: at start and when the stream starts or ends. Never raises.

    During a stream it counts as a game event: a bot restarted mid-stream shows the titles
    for their hold instead of hiding them until the next move.
    """
    global _active_at
    if not Roll.OVERLAY_ENABLED:
        return
    try:
        version = _titles_version
        standings = await game.titles(session_id)
        _state['live'] = live
        if version == _titles_version:
            _state['titles'] = _titles(standings.loser, standings.champion)
        if live:
            _active_at = time.monotonic()
        feed.publish(_snapshot())
    except Exception:
        logger.exception('Состояние игры для оверлея не прочитано')


async def _page(_request: web.Request) -> web.Response:
    # Read on every request: a page edit reaches OBS with a refresh, without a restart
    return web.Response(text=PAGE.read_text(encoding='utf-8'), content_type='text/html',
                        headers={'Cache-Control': 'no-cache', **CORS})


async def _to_page(request: web.Request) -> web.Response:
    # The page's parameters (layout, scale, titles…) go along with the redirect
    raise web.HTTPFound(request.rel_url.with_path('/roll/').with_query(request.rel_url.query))


def add_routes(app: web.Application) -> None:
    app.router.add_get('/roll', _to_page)
    app.router.add_get('/roll/', _page)
    app.router.add_get('/roll/events', feed.events)
    app.router.add_get('/roll/state', feed.state)
