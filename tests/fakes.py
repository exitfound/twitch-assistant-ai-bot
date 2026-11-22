"""Stand-ins for the twitchio objects and the Bot that handlers touch."""
import itertools
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock


def make_chatter(name: str = 'viewer', *, broadcaster=False, moderator=False,
                 subscriber=False, founder=False, vip=False) -> SimpleNamespace:
    """The five badges the bot reads, plus a name and an id. As in twitchio, the
    broadcaster counts as a moderator."""
    return SimpleNamespace(
        name=name, id=f'id-{name}', broadcaster=broadcaster, moderator=moderator or broadcaster,
        subscriber=subscriber, founder=founder, vip=vip,
    )


_message_ids = itertools.count(1)


def make_message(text: str, chatter: SimpleNamespace | None = None, *, reply=None) -> SimpleNamespace:
    return SimpleNamespace(id=f'msg-{next(_message_ids)}', text=text, chatter=chatter or make_chatter(),
                           reply=reply, respond=AsyncMock())


class FakeBot:
    """The part of Bot that handlers touch: session, stream state, cooldowns, sending, clips,
    and the Helix follower lookup the gate makes."""

    def __init__(self, *, session_id: str = '2026-09-22 20:00', stream_live: bool = True) -> None:
        self.session_id = session_id
        self.stream_live = stream_live
        self.bot_id = '1000'
        self.channel_id = '2000'
        # What Helix answers about any viewer's follow: True, False or an error to raise
        self.follows: bool | Exception = True
        self.follow_checks = 0
        self.bot_name = 'sosuryan_bot'
        self.rewards_active = False
        self.send_chat_message = AsyncMock(return_value=True)
        self.create_clip = AsyncMock(return_value='https://clips.twitch.tv/FakeClip')
        # Who Twitch says is in chat; None – the list did not come
        self.fetch_chatters = AsyncMock(return_value=None)
        # The channel's schedule as Helix gives it; None – Twitch did not say
        self.fetch_schedule = AsyncMock(return_value={})
        self._cooldowns: dict[str, float] = {}

    def create_partialuser(self, user_id: str) -> SimpleNamespace:
        return SimpleNamespace(fetch_followers=self._fetch_followers)

    async def _fetch_followers(self, *, user: str, token_for: str) -> SimpleNamespace:
        self.follow_checks += 1
        if isinstance(self.follows, Exception):
            raise self.follows

        async def first_page() -> list:
            # twitchio's followers is an iterator whose await gives the first page
            return [SimpleNamespace(user=user)] if self.follows else []
        return SimpleNamespace(followers=first_page())

    def cooldown_remaining(self, user: str, scope: str) -> float:
        return max(0.0, self._cooldowns.get(f'{scope}:{user}', 0.0) - time.time())

    def set_cooldown(self, user: str, seconds: int, scope: str) -> None:
        self._cooldowns[f'{scope}:{user}'] = time.time() + seconds

    def clear_cooldown(self, user: str, scope: str) -> None:
        self._cooldowns.pop(f'{scope}:{user}', None)


def always(monkeypatch, flask) -> None:
    """The random flask always comes up as `flask`. A swap needs a free partner before the draw;
    for any other flask a lone test player gets one, so a test of that flask needs no extra player.
    The rule itself is tested on its own."""
    from src.roll import game
    monkeypatch.setattr(game, 'FLASKS', {flask: 1})
    if flask == game.Flask.SWAP:
        monkeypatch.setattr(game, '_partners', _REAL_PARTNERS.setdefault('partners', game._partners))
        return
    real = _REAL_PARTNERS.setdefault('partners', game._partners)

    async def partners(session_id, actor, row):
        return await real(session_id, actor, row) or [('', 0)]
    monkeypatch.setattr(game, '_partners', partners)


_REAL_PARTNERS: dict = {}


async def play(handler, ctx) -> None:
    """Run a game handler on a chat command the way the bot's link does: the replies go to
    the message, what the game gives back is applied to the bot's gate."""
    import dataclasses

    from src.roll.request import Request
    from src.twitch.core.replies import reply
    from src.twitch.core.viewer import tier_of
    from src.twitch.roll.client import apply_gate
    request = Request(
        user=ctx.user, tier=tier_of(ctx.message.chatter), session_id=ctx.session_id, bot=ctx.bot,
        send=lambda text: reply(ctx.message, text), args=ctx.args, original_args=ctx.original_args,
    )
    await handler(request)
    await apply_gate(ctx, ctx.prompt.split()[0], **dataclasses.asdict(request.gate))


def chat_component(bot, followers=None, *, game: bool = True):
    """The dispatcher as the bot builds it, with the game linked in this process."""
    from src.twitch.core.component import ChatComponent
    from src.twitch.core.followers import FollowerCache
    from src.twitch.roll.client import LocalRoll
    followers = followers if followers is not None else FollowerCache()
    return ChatComponent(bot, followers, LocalRoll(bot, followers) if game else None)
