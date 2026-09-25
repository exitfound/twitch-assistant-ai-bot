"""Twitch user tokens: the OAuth links, the bot's and the channel's tokens, saving to disk."""
import logging
from pathlib import Path

import aiohttp
import twitchio
from twitchio.ext import commands

from src.core.chat_socket import stored_token
from src.core.config import Twitch

logger = logging.getLogger(__name__)

OAUTH_SCOPES = 'user:read:chat+user:write:chat+user:bot+clips:edit'
OAUTH_SCOPES_FOLLOWS = f'{OAUTH_SCOPES}+moderator:read:followers'

# Where twitchio keeps the tokens: the name it uses by default, next to the working
# directory. In the container that is the /data volume
TOKENS_FILE = '.tio.tokens.json'


def oauth_link(scopes: str) -> str:
    """The login link twitchio's OAuth adapter serves, plus how to reach it from a container."""
    return (f'http://localhost:4343/oauth?scopes={scopes}&force_verify=true\n'
            '(в контейнере ссылка не откроется – запусти make oauth на хосте)')


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
