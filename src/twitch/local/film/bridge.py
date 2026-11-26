"""The chat bridge Twitch → cinema hall while a session is open (docs/KINOZAL.md «Мост чатов»).

Every Twitch line – the viewers' and the bot's own, not commands – goes to the gate's chat API
as `texts.film_bridge_to_hall`, with the author's colour, badges and the line's emotes (look()).
A hall line comes to Twitch from the gate as the viewer's own, starting with FILM_HALL_MARK:
from_hall() takes the mark off, so it is read like any line and does not go back to the hall.
"""
import asyncio
import logging

from src.core.config import Film
from src.core.content import Content
from src.twitch.local.film import hall as film

logger = logging.getLogger(__name__)

# Lines waiting for the gate; a full queue drops the new line with a warning
OUTBOX_MAX = 100

_outbox: asyncio.Queue[dict] | None = None


def _queue() -> asyncio.Queue[dict]:
    global _outbox
    if _outbox is None:
        _outbox = asyncio.Queue(OUTBOX_MAX)
    return _outbox


def from_hall(text: str) -> str | None:
    """The line without the hall's mark if it came from the hall, otherwise None.

    Not tied to FILM_BRIDGE: hall lines come to Twitch from the gate whatever the bot bridges.
    """
    mark = Film.HALL_MARK
    if Film.ENABLED and mark and text.startswith(mark):
        return text[len(mark):].strip()
    return None


def look(message) -> tuple[str | None, dict[str, str], list[str]]:
    """The author's colour (#rrggbb, None – not chosen), the line's emotes {name: id}, the badges ["set/version"]."""
    colour = getattr(message, 'colour', None)
    color = getattr(colour, 'html', None) if colour is not None else None
    emotes: dict[str, str] = {}
    for fragment in getattr(message, 'fragments', None) or []:
        emote = getattr(fragment, 'emote', None)
        if getattr(fragment, 'type', None) == 'emote' and emote is not None and fragment.text:
            emotes[fragment.text] = str(emote.id)
    badges = [f'{b.set_id}/{b.id}' for b in getattr(message, 'badges', None) or []
              if getattr(b, 'set_id', None) and getattr(b, 'id', None)]
    return color, emotes, badges


def from_twitch(name: str, text: str, color: str | None = None, emotes: dict[str, str] | None = None,
                badges: list[str] | None = None, login: str | None = None) -> None:
    """A Twitch line, a viewer's or the bot's own: queued for the hall while a session is open. Never waits.

    `color`, `emotes` and `badges` come from look(); the gate keeps them for the hall's page. `name` is
    the display name; `login` lets the gate match a hall ban, which it keeps by login.
    """
    if not Film.BRIDGE or not film.is_open():
        return
    text = ' '.join(text.split())
    if not text or text.startswith('!'):
        return
    # A hall line the gate posted as the viewer: it does not go back
    if Film.HALL_MARK and text.startswith(Film.HALL_MARK):
        return
    line: dict = {'body': Content.text('film_bridge_to_hall', user=name, text=text)}
    if color:
        line['color'] = color
    if emotes:
        line['emotes'] = emotes
    if badges is not None:  # [] too: the author has no badges now, the hall drops the old ones
        line['badges'] = badges
    if login:
        line['login'] = login
    try:
        _queue().put_nowait(line)
    except asyncio.QueueFull:
        logger.warning('Мост кинозала: очередь в кинозал полна, строка пропущена')


async def bridge_loop() -> None:
    """Twitch → hall, one line at a time, in order, for as long as the bot runs."""
    queue = _queue()
    failing = False
    while True:
        line = await queue.get()
        try:
            await film.request('POST', film.CHAT_PATH, line)
        except film.GateError as error:
            if not failing:
                logger.warning('Мост кинозала: строка в кинозал не ушла (%s)', error)
            failing = True
        else:
            failing = False
