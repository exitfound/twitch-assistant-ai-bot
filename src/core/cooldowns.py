"""Cooldown expiries, keyed by scope and viewer, on the monotonic clock.

time.monotonic() rather than time.time(): a jump of the system clock (NTP, a host
suspend) must neither stretch a 30-second wait into hours nor clear it.
"""
import time

# The store is pruned of expired entries once it grows past this
PRUNE_AT = 500


class Cooldowns:
    """Scopes are independent: waiting out a Gemini command does not block !help-bot.

    The scope is the command's class (Kind.LOCAL / Kind.GEMINI), or a scope of its own
    for the follow hint and the refusals, always passed explicitly.
    """

    def __init__(self) -> None:
        self._expiry: dict[str, float] = {}

    def remaining(self, user: str, scope: str) -> float:
        expiry = self._expiry.get(f'{scope}:{user}')
        if expiry is None:
            return 0.0
        return max(0.0, expiry - time.monotonic())

    def set(self, user: str, seconds: int, scope: str) -> None:
        now = time.monotonic()
        if len(self._expiry) > PRUNE_AT:
            self._expiry = {k: e for k, e in self._expiry.items() if e > now}
        self._expiry[f'{scope}:{user}'] = now + seconds

    def clear(self, user: str, scope: str) -> None:
        self._expiry.pop(f'{scope}:{user}', None)
