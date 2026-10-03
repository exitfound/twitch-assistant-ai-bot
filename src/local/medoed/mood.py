"""The rule behind the medoed overlay: how many people wrote lately decides the pose."""
import time

from src.core.config import Medoed

# lying, sitting, standing, dancing – the overlay passes them in this order both ways
MOODS = ('sad', 'bored', 'idle', 'dance')


def target(chatters: int) -> tuple[str, float]:
    """The mood and the dance speed this many distinct chatters ask for."""
    if chatters >= Medoed.DANCE:
        # one tenth faster per chatter above the threshold, in whole tenths
        return 'dance', min(round(1 + 0.1 * (chatters - Medoed.DANCE), 1), Medoed.MAX_RATE)
    if chatters >= Medoed.STAND:
        return 'idle', 1.0
    if chatters >= Medoed.SIT:
        return 'bored', 1.0
    return 'sad', 1.0


class MoodTracker:
    """Who wrote and when, and the mood the overlay shows.

    Up goes at once. Down goes one level at a time, each after STEP_DOWN_SECONDS below the
    current level: an emptied chat walks the medoed back pose by pose, not in one drop.
    """

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._low_since: float | None = None
        self.mood = MOODS[0]
        self.rate = 1.0
        self.chatters = 0

    def saw(self, nick: str, now: float | None = None) -> None:
        self._seen[nick.lower()] = time.monotonic() if now is None else now

    def update(self, now: float | None = None) -> bool:
        """Recount and move the mood. True when the overlay has to be told."""
        now = time.monotonic() if now is None else now
        cutoff = now - Medoed.WINDOW_SECONDS
        self._seen = {nick: at for nick, at in self._seen.items() if at > cutoff}
        self.chatters = len(self._seen)
        want, rate = target(self.chatters)
        before = (self.mood, self.rate)
        level, wanted = MOODS.index(self.mood), MOODS.index(want)
        if wanted >= level:
            self.mood = want
            self._low_since = None
        else:
            if self._low_since is None:
                self._low_since = now
            if now - self._low_since >= Medoed.STEP_DOWN_SECONDS:
                self.mood = MOODS[level - 1]
                self._low_since = None if self.mood == want else now
        # speed only while the chat itself still asks for the dance
        self.rate = rate if self.mood == want == 'dance' else 1.0
        return (self.mood, self.rate) != before

    def state(self) -> dict:
        return {'mood': self.mood, 'rate': self.rate, 'chatters': self.chatters}


tracker = MoodTracker()
