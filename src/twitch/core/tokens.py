"""Twitch user tokens: the OAuth links, the bot's and the channel's tokens, saving to disk."""
import logging
from pathlib import Path

import aiohttp
import twitchio
from aiohttp import web
from twitchio.ext import commands

from src.twitch.core.chat_socket import stored_token
from src.core.config import Twitch

logger = logging.getLogger(__name__)

OAUTH_SCOPES = 'user:read:chat+user:write:chat+user:bot+clips:edit'
# What the bot reads as the channel's moderator: followers for the follow gate, the chat list
# for the mass flasks. A login link always asks for all of it
OAUTH_SCOPES_FULL = f'{OAUTH_SCOPES}+moderator:read:followers+moderator:read:chatters'

# Short login links on twitchio's OAuth server: the full /oauth link is long enough for a
# terminal to wrap it, and a copy then loses the scopes after the break
LOGIN_HOST = 'http://localhost:4343'
BOT_LOGIN = '/bot'
CHANNEL_LOGIN = '/channel'

# Where twitchio keeps the tokens: the name it uses by default, next to the working
# directory. In the container that is the /data volume
TOKENS_FILE = '.tio.tokens.json'


def oauth_path(scopes: str) -> str:
    """twitchio's own login route with these scopes, always asking which account logs in."""
    return f'/oauth?scopes={scopes}&force_verify=true'


def add_login_routes(adapter: object, channel_scopes: str) -> None:
    """BOT_LOGIN and CHANNEL_LOGIN on twitchio's OAuth server: redirects to its /oauth with the
    bot's every scope or the channel's. Skipped on an adapter that is not an aiohttp app."""
    router = getattr(adapter, 'router', None)
    if router is None:
        return
    for path, scopes in ((BOT_LOGIN, OAUTH_SCOPES_FULL), (CHANNEL_LOGIN, channel_scopes)):
        target = oauth_path(scopes)

        async def login(request: web.Request, target: str = target) -> web.Response:
            raise web.HTTPFound(target)
        router.add_get(path, login)


def oauth_link(path: str) -> str:
    """A short login link plus how to reach it from a container, for the log."""
    return f'{LOGIN_HOST}{path}\n(в контейнере ссылка не откроется – запусти make oauth на хосте)'


def print_login_links() -> None:
    """Both short login links, for `make oauth`."""
    print(f'Вход под аккаунтом бота: {LOGIN_HOST}{BOT_LOGIN}')
    print(f'Вход под аккаунтом канала (награды за баллы): {LOGIN_HOST}{CHANNEL_LOGIN}')


def token_problem(error: Exception) -> bool:
    """Whether a failed chat subscription may be about the bot's token.

    Only then is the OAuth link worth printing: a network error, a Twitch outage or a
    duplicate subscription would send the owner to re-authorise for nothing. An error of
    an unknown kind still gets the hint – a missing token is not an HTTP error.
    """
    if isinstance(error, twitchio.HTTPException):
        return error.status in (401, 403)
    # TimeoutError is an OSError
    return not isinstance(error, (OSError, aiohttp.ClientError))


def token_dead(error: Exception) -> bool:
    """Whether a Twitch error means the user's authorization is gone and only a new login helps.

    A refresh token Twitch refuses (400 from id.twitch.tv), a token twitchio could not
    validate, a 401 or 403 left after twitchio's own refresh. A network error or a 5xx is
    not: retrying those is right, retrying a revoked token hammers Twitch for nothing.
    """
    if isinstance(error, twitchio.InvalidTokenException):
        return True
    if not isinstance(error, twitchio.HTTPException):
        return False
    if error.status in (401, 403):
        return True
    # use_id: a request to id.twitch.tv, where the refresh and the validation go
    return error.status == 400 and error.route is not None and error.route.use_id


async def add_bot_token(bot: commands.Bot) -> None:
    """Add the bot's token from .env unless twitchio already loaded one from .tio.tokens.json.

    The file is the fresher of the two: twitchio writes every refresh and every new login
    there, while .env keeps the value it was given. Adding .env on top would bring back the
    old token and drop the scopes a new login granted.
    """
    if stored_token(bot, str(bot.bot_id)) is None and Twitch.BOT_TOKEN and Twitch.BOT_REFRESH:
        await bot.add_token(Twitch.BOT_TOKEN, Twitch.BOT_REFRESH)


async def add_broadcaster_token(bot: commands.Bot, channel_id: str, scope: str) -> bool:
    """Add the channel's token. True – it belongs to the channel and carries the scope.

    Prefers what twitchio loaded from .tio.tokens.json over the .env values, which
    twitchio does not update after a refresh; falls back to .env when it is empty.
    Without the check a foreign or under-scoped token surfaces only on the first
    redemption, as an obscure Twitch error in the middle of a stream.
    """
    stored = stored_token(bot, channel_id)
    if stored:
        token, refresh = stored['token'], stored['refresh']
    elif Twitch.BROADCASTER_TOKEN and Twitch.BROADCASTER_REFRESH:
        token, refresh = Twitch.BROADCASTER_TOKEN, Twitch.BROADCASTER_REFRESH
    else:
        return False
    try:
        payload = await bot.add_token(token, refresh)
    except Exception as e:
        logger.warning('Токен канала не принят, награды за баллы выключены: %s', e)
        return False
    if str(payload.user_id) != channel_id:
        logger.warning(
            'TWITCH_BROADCASTER_TOKEN выдан аккаунту %s, а не каналу %s – награды за баллы выключены',
            payload.login, Twitch.CHANNEL,
        )
        return False
    if scope not in payload.scopes:
        logger.warning('В токене канала нет права %s – награды за баллы выключены', scope)
        return False
    return True


async def store_tokens(bot: commands.Bot, whose: str, env_name: str,
                       payload: twitchio.authentication.UserTokenPayload) -> None:
    """Save the tokens to disk and log that, without putting them in the output.

    A token printed in full lands in `docker logs`, which keeps it on disk across
    restarts. It is written where twitchio reads it from anyway, the file is set to
    mode 0600, and the log gets the last four characters to tell tokens apart.
    """
    await bot.save_tokens()
    path = Path(TOKENS_FILE)
    try:
        path.chmod(0o600)
    except OSError as e:
        logger.warning('Не удалось ограничить права на %s: %s', path, e)
    logger.info(
        'Токен %s получен (…%s) и сохранён в %s – следующий запуск возьмёт его оттуда. '
        '%s в .env нужен, только если этого файла не будет',
        whose, payload.access_token[-4:], path, env_name,
    )
