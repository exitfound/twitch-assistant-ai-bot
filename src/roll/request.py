"""A game command as the game sees it: who asked, with what, and what the bot's gate must undo.

The bot's dispatcher has already passed the command through its gate (follow, cooldown,
quota). Replies go out through send() as they are made, so their order in chat is the
order the handler wrote them; what the gate must give back is reported with the result.
"""
import dataclasses
from collections.abc import Awaitable, Callable

from src.core.tier import Tier
from src.roll.port import RollPort

# Cooldown scope of refusals by a limit: one per 30 s per viewer, not one per message
DENY_SCOPE = 'deny'
DENY_REPEAT_SECONDS = 30


@dataclasses.dataclass
class Gate:
    """What the bot's gate must undo after the handler."""
    refuse: bool = False            # give back the cooldown and the quota row
    clear_cooldown: bool = False    # give back the cooldown only
    clear_public: bool = False      # give back the trigger's shared public cooldown


@dataclasses.dataclass
class Request:
    user: str
    tier: Tier
    session_id: str
    bot: RollPort
    send: Callable[[str], Awaitable[bool]]
    args: str = ''
    original_args: str = ''
    gate: Gate = dataclasses.field(default_factory=Gate)

    @property
    def broadcaster(self) -> bool:
        return self.tier == Tier.BROADCASTER

    async def reply(self, text: str) -> bool:
        return await self.send(text)

    def refuse(self) -> None:
        """Malformed input: the viewer got no game, so the cooldown and the quota row go back."""
        self.gate.refuse = True

    def clear_cooldown(self) -> None:
        """Nothing was played: the cooldown goes back."""
        self.gate.clear_cooldown = True

    def clear_public(self) -> None:
        """A public command refused its input: its shared pause goes back to the chat."""
        self.gate.clear_public = True

    async def deny(self, text: str) -> None:
        """A refusal by a limit, said at most once per DENY_REPEAT_SECONDS to a viewer."""
        if self.bot.cooldown_remaining(self.user, DENY_SCOPE):
            return
        self.bot.set_cooldown(self.user, DENY_REPEAT_SECONDS, DENY_SCOPE)
        await self.reply(text)
