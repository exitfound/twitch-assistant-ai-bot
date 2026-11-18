"""The chat bridge Twitch → cinema hall, while a session is open.

Every Twitch line – the viewers' and the bot's own, not commands – goes to the gate,
which posts it to the hall's chat from the author's twin «nick [TW]» (`texts.film_bridge_to_hall`).
With the line go the author's chat colour, badges and the line's emotes (look()): the hall shows
the nick in the same colour with the same badges, and the emotes as pictures.

The other way round is the gate's: a line from the hall by someone who signed in with
Twitch goes to Twitch from that viewer's own account, starting with FILM_HALL_MARK. The
bot sees it in chat like any other viewer's line – it is one: from_hall() takes the mark
off, so a command or a call to the bot from the hall works as in Twitch, and the line
does not go back to the hall. The bot's answer is a Twitch line, so the hall sees it.
"""
import asyncio
import logging

from src.core.config import Film
from src.core.content import Content
from src.twitch.local import film

logger = logging.getLogger(__name__)

OUTBOX_MAX = 100

_outbox: asyncio.Queue[dict] | None = None


def _queue() -> asyncio.Queue[dict]:
    global _outbox
    if _outbox is None:
        _outbox = asyncio.Queue(OUTBOX_MAX)
    return _outbox


def from_hall(text: str) -> str | None:
    """The line without the hall's mark if it came from the hall, otherwise None."""
    mark = Film.HALL_MARK
    if Film.BRIDGE and mark and text.startswith(mark):
        return text[len(mark):].strip()
    return None


def look(message) -> tuple[str | None, dict[str, str], list[str]]:
    """The author's chat colour (#rrggbb, None – not chosen), the line's emotes {name: id} and the
    author's badges ["moderator/1", "subscriber/12", ...] as Twitch shows them."""
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
                badges: list[str] | None = None) -> None:
    """A Twitch viewer's line: queued for the hall while a session is open. Never waits.

    `color`, `emotes` and `badges` (from look()) go along for the hall's look: the gate keeps them.
    """
    if not Film.BRIDGE or not film.is_open():
        return
    text = ' '.join(text.split())
    if not text or text.startswith('!'):
        return
    # Came from the hall in the first place (the gate posted it as the viewer)
    if Film.HALL_MARK and text.startswith(Film.HALL_MARK):
        return
    line: dict = {'body': Content.text('film_bridge_to_hall', user=name, text=text)}
    if color:
        line['color'] = color
    if emotes:
        line['emotes'] = emotes
    if badges is not None:  # [] too: the badges were taken off, the hall drops them
        line['badges'] = badges
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
