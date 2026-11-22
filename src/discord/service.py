"""The voice service's API: the bot sends here every answer that reached chat."""
from aiohttp import web

from src.core import speech
from src.core.config import Service
from src.core.service import require_token


def app() -> web.Application:
    app = web.Application(middlewares=[require_token(Service.TOKEN)])
    app.router.add_post('/speech', _speech)
    return app


async def _speech(request: web.Request) -> web.Response:
    """Publish the answer to the speaker in this process; it queues or drops it at once."""
    body = await request.json()
    speech.say(str(body.get('text') or ''), str(body.get('source') or 'twitch'))
    return web.json_response({})
