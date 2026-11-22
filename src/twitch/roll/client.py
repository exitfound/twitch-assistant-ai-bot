"""The bot's link to the game service (src/roll/): what it asks the game and what it tells it.

RollLink is the contract; a call that needs an answer raises RollUnavailable when the game
cannot give one, so the caller decides what the chat hears. RollClient carries it over the
game's internal API.
"""
import asyncio
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Protocol

from src.core.config import Service
from src.core.service import ServiceClient, ServiceUnavailable
from src.core.tier import Tier
from src.core.utils import put_latest
from src.twitch.core.commands import PUBLIC_SCOPE, CommandContext
from src.twitch.core.viewer import tier_of

if TYPE_CHECKING:
    from src.twitch.core.api import BotApi
    from src.twitch.core.port import BotPort

logger = logging.getLogger(__name__)

# A command: the board reads a month of streams
COMMAND_SECONDS = 10
# A redemption: a mass flask waits up to 5 s for Twitch's chat list inside it
REDEEM_SECONDS = 20
# Chat and stream events waiting for the game: a game that is down loses the oldest
EVENTS_QUEUE = 256
# A stream event is retried while the game restarts: it carries the stream start's perks
STREAM_RETRIES = 12
STREAM_RETRY_SECONDS = 5


class RollUnavailable(Exception):
    """The game did not answer: down or restarting. Nothing was done."""


class RollSlow(Exception):
    """The game did not answer, but the request reached it: it may have done the work."""


@dataclasses.dataclass(frozen=True)
class Spec:
    """A channel-points reward as the game defines it, every field rendered."""
    action: str
    title: str
    prompt: str
    cost: int
    input_required: bool
    max_per_user: int           # per stream, 0 – no limit


@dataclasses.dataclass(frozen=True)
class GameCommand:
    """A command the game declares: the bot registers it and carries it to the game."""
    trigger: str
    public: bool = False        # a shared pause for the whole chat, no follow check

    def as_dict(self) -> dict:
        return {'trigger': self.trigger, 'public': self.public}


class RollLink(Protocol):

    async def commands(self) -> list[GameCommand]: ...

    async def command(self, trigger: str, ctx: CommandContext) -> None:
        """Run a game command; the replies go to chat, the gate's refunds are applied to ctx."""

    async def chat(self, session_id: str, user: str, tier: Tier) -> None:
        """A chat message: the game's perks and the viewer's status. Never raises."""

    async def stream(self, session_id: str) -> None:
        """The stream started or ended, or the bot started: session_id – the stream it is
        about (the one that ended, on an end). Never raises."""

    async def specs(self) -> list[Spec]: ...

    async def redeem(self, action: str, redemption_id: str, user: str, user_id: str,
                     user_input: str) -> bool | None:
        """True – fulfill, False – refund, None – a repeat, leave the status alone.
        RollSlow – no answer in time: the game's journal tells later."""

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


class RollClient:
    """RollLink over the game's internal API. Events go out in order through one queue,
    so the chat path never waits for the game."""

    def __init__(self, bot: 'BotPort', api: 'BotApi') -> None:
        self._bot = bot
        self._api = api
        self._client = ServiceClient('игра', Service.ROLL_URL or '', Service.TOKEN)
        self._events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(EVENTS_QUEUE)
        # Logged once per outage, not once per chat message
        self._down = False

    def _state(self, session_id: str | None = None) -> dict:
        bot = self._bot
        # «at» orders states: a queued event carries the state of its moment, and the game
        # must not let it undo a fresher one from a direct call
        return {'session_id': session_id or bot.session_id, 'live': bot.stream_live,
                'rewards_active': bot.rewards_active, 'at': time.time()}

    async def command(self, trigger: str, ctx: CommandContext) -> None:
        body = {
            'state': self._state(ctx.session_id), 'trigger': trigger, 'user': ctx.user,
            'tier': tier_of(ctx.message.chatter), 'message_id': ctx.message.id,
            'args': ctx.args, 'original_args': ctx.original_args,
        }
        # The game replies through the bot's API while the call runs: the message waits there
        with self._api.hold(ctx.message):
            try:
                gate = await self._client.call('POST', '/command', body, seconds=COMMAND_SECONDS)
            except ServiceUnavailable as e:
                if e.maybe_done:
                    # The game may have answered already: no «unavailable» on top of its reply
                    logger.warning('Игра не ответила на %s от %s, но могла выполнить: %s', trigger, ctx.user, e)
                    return
                raise RollUnavailable(str(e)) from e
        await apply_gate(ctx, trigger, refuse=bool(gate.get('refuse')),
                         clear_cooldown=bool(gate.get('clear_cooldown')),
                         clear_public=bool(gate.get('clear_public')))

    async def chat(self, session_id: str, user: str, tier: Tier) -> None:
        body = {'state': self._state(session_id), 'user': user, 'tier': str(tier)}
        put_latest(self._events, ('/events/chat', body))

    async def stream(self, session_id: str) -> None:
        put_latest(self._events, ('/events/stream', {'state': self._state(), 'session_id': session_id}))

    async def events_loop(self) -> None:
        """Send the queued events in order, for as long as the bot runs."""
        while True:
            path, body = await self._events.get()
            try:
                await self._send(path, body)
            finally:
                self._events.task_done()

    async def _send(self, path: str, body: dict) -> None:
        tries = STREAM_RETRIES if path == '/events/stream' else 1
        for attempt in range(tries):
            try:
                await self._client.call('POST', path, body, seconds=COMMAND_SECONDS)
            except ServiceUnavailable as e:
                if not self._down:
                    logger.warning('Игра недоступна – события чата и эфира до неё не доходят: %s', e)
                    self._down = True
                if attempt + 1 < tries:
                    await asyncio.sleep(STREAM_RETRY_SECONDS)
                continue
            if self._down:
                logger.info('Игра снова на связи')
                self._down = False
            return
        if path == '/events/stream':
            logger.error('Игра так и не узнала о смене эфира (%s)', body['session_id'])

    async def commands(self) -> list[GameCommand]:
        try:
            answer = await self._client.call('GET', '/commands')
            return [GameCommand(**command) for command in answer['commands']]
        except (ServiceUnavailable, KeyError, TypeError) as e:
            raise RollUnavailable(str(e)) from e

    async def specs(self) -> list[Spec]:
        try:
            answer = await self._client.call('GET', '/rewards/specs')
            return [Spec(**spec) for spec in answer['specs']]
        except (ServiceUnavailable, KeyError, TypeError) as e:
            raise RollUnavailable(str(e)) from e

    async def redeem(self, action: str, redemption_id: str, user: str, user_id: str,
                     user_input: str) -> bool | None:
        body = {'state': self._state(), 'action': action, 'redemption_id': redemption_id,
                'user': user, 'user_id': str(user_id), 'input': user_input or ''}
        try:
            answer = await self._client.call('POST', '/redemption', body, seconds=REDEEM_SECONDS)
        except ServiceUnavailable as e:
            # Sent, but no answer: refunding now could give an applied reward for free
            if e.maybe_done:
                raise RollSlow(str(e)) from e
            raise RollUnavailable(str(e)) from e
        return answer.get('decision')

    async def applied(self, redemption_ids: list[str]) -> set[str]:
        try:
            return set((await self._client.call('POST', '/rewards/applied', {'ids': redemption_ids}))['applied'])
        except (ServiceUnavailable, KeyError) as e:
            raise RollUnavailable(str(e)) from e

    async def close(self) -> None:
        await self._client.close()
