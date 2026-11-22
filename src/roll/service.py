"""The game service's API: what the bot sends it and asks of it.

Every call from the bot carries the stream state (session, live, rewards), so the game
never acts on a session older than the event that reached it.
"""
import dataclasses
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from src.core.config import Service
from src.core.service import require_token
from src.core.tier import Tier
from src.roll import announce, board, overlay, perks, tiers
from src.roll.command import handle_roll, handle_rollstat
from src.roll.port import RollPort
from src.roll.redemption import handle_redemption
from src.roll.remote import RemoteBot
from src.roll.request import Request
from src.roll.specs import specs
from src.roll.storage import get_applied

logger = logging.getLogger(__name__)

Handler = Callable[[Request], Awaitable[None]]


@dataclasses.dataclass(frozen=True)
class Command:
    trigger: str
    handler: Handler
    # One shared pause for the whole chat and no follow check, like help
    public: bool = False


# The game's commands, declared to the bot by /commands: the bot registers what it is told
COMMANDS = (
    Command('!rollstat', handle_rollstat),
    # The month's board is the same for everyone and sits on the stream for a while
    Command(board.TRIGGER, board.handle_rollboard, public=True),
    Command('!roll', handle_roll),
)
HANDLERS: dict[str, Handler] = {command.trigger: command.handler for command in COMMANDS}


async def on_stream(bot: RollPort, session_id: str, *, live: bool) -> None:
    """The stream started, ended, or the bot started: titles, regalia, old curses, perks.

    Safe to repeat: every step is idempotent, and the champion's Благодать, held back
    while the rewards were down, is granted once they come up and the bot calls this again.
    """
    await overlay.refresh(session_id, live=live)
    await board.regalia(session_id, live=live)
    if live:
        # The curses of finished streams go first: the perks granted below lay new ones
        await announce.on_stream_start(session_id)
        await perks.on_stream_start(bot, session_id)


class GameService:

    def __init__(self, bot: RemoteBot) -> None:
        self._bot = bot

    def app(self) -> web.Application:
        app = web.Application(middlewares=[require_token(Service.TOKEN)])
        app.router.add_get('/commands', self._commands)
        app.router.add_post('/command', self._command)
        app.router.add_post('/events/chat', self._chat)
        app.router.add_post('/events/stream', self._stream)
        app.router.add_get('/rewards/specs', self._specs)
        app.router.add_post('/redemption', self._redemption)
        app.router.add_post('/rewards/applied', self._applied)
        return app

    async def _body(self, request: web.Request) -> dict:
        body = await request.json()
        self._bot.update(body['state'])
        return body

    async def _commands(self, _request: web.Request) -> web.Response:
        return web.json_response({'commands': [{'trigger': c.trigger, 'public': c.public} for c in COMMANDS]})

    async def _command(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        handler = HANDLERS.get(body['trigger'])
        if handler is None:
            return web.json_response({'error': 'unknown command'}, status=404)
        message_id = str(body['message_id'])
        session_id, user, tier = body['state']['session_id'], body['user'], Tier(body['tier'])
        # The message's chat event waits in the bot's queue, and the command may overtake it:
        # a first «!roll» would start a perk silently. Run it here first; the late one finds
        # nothing new
        await perks.on_chat(self._bot, session_id, user)
        await tiers.on_chat(self._bot, session_id, user, tier)
        ctx = Request(
            user=user, tier=tier, session_id=session_id, bot=self._bot,
            send=lambda text, deny: self._bot.reply(message_id, text, deny=deny),
            args=body.get('args', ''), original_args=body.get('original_args', ''),
        )
        try:
            await handler(ctx)
        except Exception:
            # The net under a bug in a handler: the viewer gets no answer, as with any command
            logger.exception('Команда %s от %s упала', body['trigger'], ctx.user)
        gate = ctx.gate
        return web.json_response({'refuse': gate.refuse, 'clear_cooldown': gate.clear_cooldown,
                                  'clear_public': gate.clear_public})

    async def _chat(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        session_id = body['state']['session_id']
        await perks.on_chat(self._bot, session_id, body['user'])
        await tiers.on_chat(self._bot, session_id, body['user'], Tier(body['tier']))
        return web.json_response({})

    async def _stream(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        # On an end the event is about the stream that ended, while the state is the date session
        await on_stream(self._bot, body['session_id'], live=self._bot.stream_live)
        return web.json_response({})

    async def _specs(self, _request: web.Request) -> web.Response:
        return web.json_response({'specs': [spec.as_dict() for spec in specs()]})

    async def _redemption(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        user_id = str(body['user_id'])
        decision = await handle_redemption(
            self._bot, body['action'], body['redemption_id'], body['user'], body.get('input') or '',
            follows=lambda: self._bot.follows(user_id),
        )
        return web.json_response({'decision': decision})

    async def _applied(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({'applied': sorted(await get_applied([str(i) for i in body['ids']]))})
