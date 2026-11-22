"""The bot's link to the game: what it asks the game and what it tells it.

RollLink is the contract; every call raises RollUnavailable when the game cannot answer,
so the caller decides what the chat hears. LocalRoll runs the game in this process.
"""
import dataclasses
from typing import TYPE_CHECKING, Protocol

from src.core.tier import Tier
from src.twitch.core.commands import PUBLIC_SCOPE, CommandContext
from src.twitch.core.followers import FollowerCache
from src.twitch.core.replies import reply
from src.twitch.core.viewer import tier_of

if TYPE_CHECKING:
    from src.twitch.core.port import BotPort


class RollUnavailable(Exception):
    """The game did not answer: down, restarting or too slow."""


@dataclasses.dataclass(frozen=True)
class Spec:
    """A channel-points reward as the game defines it, every field rendered."""
    action: str
    title: str
    prompt: str
    cost: int
    input_required: bool
    max_per_user: int           # per stream, 0 – no limit


class RollLink(Protocol):

    async def command(self, trigger: str, ctx: CommandContext) -> None:
        """Run a game command; the replies go to chat, the gate's refunds are applied to ctx."""

    async def chat(self, session_id: str, user: str, tier: Tier) -> None:
        """A chat message: the game's perks and the viewer's status. Never raises."""

    async def stream(self, session_id: str, *, live: bool, rewards_active: bool) -> None:
        """The stream state changed or the bot started. Never raises."""

    async def specs(self) -> list[Spec]: ...

    async def redeem(self, action: str, redemption_id: str, user: str, user_id: str,
                     user_input: str) -> bool | None:
        """True – fulfill, False – refund, None – a repeat, leave the status alone."""

    async def applied(self, redemption_ids: list[str]) -> set[str]:
        """Those of the redemptions the game has applied."""


async def apply_gate(ctx: CommandContext, trigger: str, *, refuse: bool, clear_cooldown: bool,
                     clear_public: bool) -> None:
    """Give back what the game said the command did not use."""
    if refuse:
        await ctx.refuse()
    elif clear_cooldown:
        ctx.clear_cooldown()
    if clear_public:
        ctx.bot.clear_cooldown(trigger, PUBLIC_SCOPE)


class LocalRoll:
    """The game in the bot's own process."""

    def __init__(self, bot: 'BotPort', followers: FollowerCache) -> None:
        self._bot = bot
        self._followers = followers

    async def command(self, trigger: str, ctx: CommandContext) -> None:
        from src.roll.service import HANDLERS
        from src.roll.request import Request
        request = Request(
            user=ctx.user, tier=tier_of(ctx.message.chatter), session_id=ctx.session_id, bot=self._bot,
            send=lambda text: reply(ctx.message, text), args=ctx.args, original_args=ctx.original_args,
        )
        await HANDLERS[trigger](request)
        gate = request.gate
        await apply_gate(ctx, trigger, refuse=gate.refuse, clear_cooldown=gate.clear_cooldown,
                   clear_public=gate.clear_public)

    async def chat(self, session_id: str, user: str, tier: Tier) -> None:
        from src.roll import perks, tiers
        await perks.on_chat(self._bot, session_id, user)
        await tiers.on_chat(self._bot, session_id, user, tier)

    async def stream(self, session_id: str, *, live: bool, rewards_active: bool) -> None:
        from src.roll.service import on_stream
        await on_stream(self._bot, session_id, live=live)

    async def specs(self) -> list[Spec]:
        from src.roll.specs import specs
        return [Spec(**spec.as_dict()) for spec in specs()]

    async def redeem(self, action: str, redemption_id: str, user: str, user_id: str,
                     user_input: str) -> bool | None:
        from src.roll.redemption import handle_redemption
        return await handle_redemption(
            self._bot, action, redemption_id, user, user_input,
            follows=lambda: self._followers.is_follower(self._bot, user_id),
        )

    async def applied(self, redemption_ids: list[str]) -> set[str]:
        from src.roll.service import applied
        return await applied(redemption_ids)
