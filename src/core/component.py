"""Chat component: message intake, command dispatcher, follow events.

The only place in core that knows about features: this is where commands from
src/gemini, src/local and src/local/roll get into the registry.
"""
import logging
import math
import re

import twitchio
from twitchio.ext import commands

from src.core.commands import (
    KIND_GEMINI, ROLE_SUB_VIP_MOD_BROADCASTER, ROLE_VIP_MOD_BROADCASTER,
    CommandContext, CommandRegistry,
)
from src.core.config import Cooldown, Follow, Picture, Quota
from src.core.content import Content
from src.core.database import (
    count_bot_uses, count_channel_bot_uses, oldest_bot_use_age, record_bot_use,
    save_chat_message,
)
from src.core.followers import FollowerCache
from src.core.utils import SOSUR_RE, SOSUR_VARIANTS, reply_to_bot  # noqa: F401  (SOSUR_VARIANTS – the public place to edit the list)
from src.core.viewer import Tier, by_tier, tier_of
from src.gemini.commands import (
    handle_ask, handle_default, handle_summary, handle_versus, handle_who,
)
from src.gemini.picture.command import handle_ascii
from src.local.commands import handle_help, handle_stats
from src.local.follow import handle_follow
from src.local.roll.command import handle_roll, handle_rollstat
from src.local.roll.perks import on_chat as roll_perks_on_chat

logger = logging.getLogger(__name__)

STATS_TRIGGER = '!stat'
HELP_TRIGGER = '!help-bot'
ASK_TRIGGER = '!ask'
SUMMARY_TRIGGER = '!summary'
WHO_TRIGGER = '!who'
VERSUS_TRIGGER = '!versus'
ROLL_TRIGGER = '!roll'
ROLLSTAT_TRIGGER = '!rollstat'
ASCII_TRIGGER = '!ascii'

# Cooldown scope for the «follow the channel» hint: so the bot does not repeat it
# on every message of a non-following viewer
FOLLOW_HINT_SCOPE = 'follow_hint'

# Same for refusals by role and by exhausted quota: the fact does not change between
# repeats, and a viewer without badges has no cooldown of their own, so without this
# brake every one of their messages gets a refusal
DENY_SCOPE = 'deny'
DENY_REPEAT_SECONDS = 30


def _quota_per_hour(tier: Tier) -> int:
    """Cap on Gemini requests per window. 0 – no limit.

    Same ladder as the cooldown: whoever paid or wears a badge is not
    limited at all. A limit on top of the cooldown guards against slow but
    constant draining: the cooldown holds the pace, the quota holds the volume.
    """
    return by_tier(tier, broadcaster=0, sub=0, vip=Quota.VIP_PER_HOUR, regular=Quota.FOLLOWER_PER_HOUR)


# Which refusal to show when the badge is not enough
ROLE_DENIED_TEXTS = {
    ROLE_VIP_MOD_BROADCASTER: 'role_denied',
    ROLE_SUB_VIP_MOD_BROADCASTER: 'role_denied_sub',
}


def _has_role(role: str | None, chatter) -> bool:
    """Whether the badges are enough for the command.

    Badges are checked directly, not through tier_of(): that one folds
    subscriber and VIP into one status for the cooldown, and a subscribing VIP
    would otherwise be refused where VIPs are let in.
    """
    if role is None:
        return True
    if chatter.broadcaster or chatter.moderator or chatter.vip:
        return True
    return role == ROLE_SUB_VIP_MOD_BROADCASTER and (chatter.subscriber or chatter.founder)


def _cooldown_seconds(tier: Tier) -> int:
    """Wait in seconds for a viewer tier. 0 – no cooldown.

    One ladder for both command classes and for free text addressed to the bot.
    Whoever paid or wears a badge does not wait at all.
    """
    return by_tier(tier, broadcaster=0, sub=0, vip=Cooldown.VIP, regular=Cooldown.REGULAR)



class ChatComponent(commands.Component):

    def __init__(self, bot):
        self.bot = bot
        self._followers = FollowerCache()
        self._registry = CommandRegistry()
        add = self._registry.add
        # All commands work as bare text, without addressing the bot.
        # Order matters: longer triggers are registered first.
        add(HELP_TRIGGER,      handle_help)
        add(STATS_TRIGGER,     handle_stats,     prefix=True)
        # Longer trigger first, by the rule above: both are exact matches,
        # so «!rollstat» never reaches !roll
        add(ROLLSTAT_TRIGGER,  handle_rollstat)
        add(ROLL_TRIGGER,      handle_roll)
        add(SUMMARY_TRIGGER,   handle_summary,   prefix=True, kind=KIND_GEMINI)
        add(WHO_TRIGGER,       handle_who,       prefix=True, kind=KIND_GEMINI)
        add(VERSUS_TRIGGER,    handle_versus,    prefix=True, kind=KIND_GEMINI)
        add(ASK_TRIGGER,       handle_ask,       prefix=True, kind=KIND_GEMINI)
        if Picture.ENABLED:
            # A disabled feature must not linger as a command that silently
            # does nothing: it simply does not exist
            add(ASCII_TRIGGER, handle_ascii, prefix=True, kind=KIND_GEMINI,
                role=ROLE_SUB_VIP_MOD_BROADCASTER)

    @commands.Component.listener()
    async def event_message(self, message: twitchio.ChatMessage) -> None:
        if str(message.chatter.id) == str(self.bot.bot_id):
            return
        if not self.bot.bot_name:
            return

        # session_id is taken once: it changes when a stream starts or ends, and coroutines outlive that
        session_id = self.bot.session_id
        # Routing runs before saving: it decides whether this is a command (commands
        # are not saved) and whether the message addressed the bot (a call word,
        # an @mention, a reply) – that is stored with the message and counted in !stat
        entry, prompt, addressed = self._route(message)
        # Only live conversation goes into the DB: bot commands (!roll, !who …) say
        # nothing about the person and clutter the Gemini context, !who and !summary
        if entry is None:
            await save_chat_message(
                session_id, message.chatter.name, message.text, addressed=addressed,
            )
        # A first appearance in the stream starts the countdown of the previous stream's game perks
        await roll_perks_on_chat(self.bot, session_id, message.chatter.name)

        if entry is None and prompt is None:
            return

        user = message.chatter.name
        chatter = message.chatter
        # Free text addressed to the bot goes to Gemini just like !ask, so it
        # shares the counter with Gemini commands, not with local ones.
        kind = entry.kind if entry is not None else KIND_GEMINI
        tier = tier_of(chatter)
        seconds = _cooldown_seconds(tier)

        # Without a follow the bot does not answer: the exception is help, which
        # is how a viewer learns what following is for
        if tier == Tier.REGULAR and not await self._allowed_without_follow(message, user, entry):
            return

        if seconds:
            remaining = self.bot.cooldown_remaining(user, kind)
            if remaining > 0:
                key = 'cooldown_gemini' if kind == KIND_GEMINI else 'cooldown_local'
                await message.respond(
                    Content.text(key, user=user, seconds=int(remaining) + 1)
                )
                return

        if entry is not None and not _has_role(entry.role, chatter):
            await self._deny(message, user, ROLE_DENIED_TEXTS[entry.role],
                             command=entry.trigger)
            return

        # The cooldown is taken right after its check, with no await in between: twitchio
        # runs every event in its own task, and two quick messages would both pass it
        if seconds:
            self.bot.set_cooldown(user, seconds, kind)

        # Quota on top of the cooldown: only Gemini requests count – they cost
        # money. Local commands are held by the cooldown alone. A refusal gives the
        # cooldown back: nothing was served
        if kind == KIND_GEMINI and not (
            await self._within_channel_quota(message, user, tier)
            and await self._within_quota(message, user, tier)
        ):
            self.bot.clear_cooldown(user, kind)
            return

        ctx = CommandContext(
            message=message,
            user=user,
            prompt=prompt,
            original_text=message.text,
            session_id=session_id,
            bot=self.bot,
            kind=kind,
            args=entry.extract_args(prompt) if entry else '',
        )

        if kind == KIND_GEMINI:
            # Recorded before generation: a failed request also cost a queue slot and money
            await record_bot_use(user, kind)

        if entry is None:
            await handle_default(ctx)
            return
        await entry.handler(ctx)

    async def _allowed_without_follow(self, message, user: str, entry) -> bool:
        """Whether to let a non-following viewer in. False – they have already been refused."""
        if not Follow.REQUIRED:
            return True
        if entry is not None and entry.trigger == HELP_TRIGGER:
            return True
        if await self._followers.is_follower(self.bot, message.chatter.id):
            return True
        # The hint repeats at most once per FOLLOW_HINT_MINUTES: otherwise command
        # spam would turn into refusal spam
        if not self.bot.cooldown_remaining(user, FOLLOW_HINT_SCOPE):
            self.bot.set_cooldown(user, Follow.HINT_MINUTES * 60, FOLLOW_HINT_SCOPE)
            await message.respond(Content.text('follow_required', user=user, command=HELP_TRIGGER))
        return False

    async def _deny(self, message, user: str, key: str, **values) -> None:
        """Refuse, but at most once per DENY_REPEAT_SECONDS per viewer.

        Refusals by role and by quota do not change from one repeat to the next,
        and a viewer without badges has no cooldown of their own – without a brake
        the bot would answer every one of their messages with a refusal.
        """
        if self.bot.cooldown_remaining(user, DENY_SCOPE):
            return
        self.bot.set_cooldown(user, DENY_REPEAT_SECONDS, DENY_SCOPE)
        await message.respond(Content.text(key, user=user, **values))

    async def _within_channel_quota(self, message, user: str, tier: Tier) -> bool:
        """Whether the channel as a whole is within its window. False – already refused.

        A ceiling over everyone, on top of the per-viewer quota, which bounds one person
        and not the channel's spend: moderators and subscribers have no personal quota at
        all. The broadcaster is exempt, and local commands keep working – only paid ones
        are refused.
        """
        if not Quota.CHANNEL_PER_HOUR or tier == Tier.BROADCASTER:
            return True
        used = await count_channel_bot_uses(KIND_GEMINI, Quota.WINDOW_MINUTES)
        if used < Quota.CHANNEL_PER_HOUR:
            return True
        logger.warning(
            'Потолок канала исчерпан: %d запросов за %d мин (лимит %d) – платные команды закрыты',
            used, Quota.WINDOW_MINUTES, Quota.CHANNEL_PER_HOUR,
        )
        await self._deny(message, user, 'quota_channel', window=Quota.WINDOW_MINUTES)
        return False

    async def _within_quota(self, message, user: str, tier: Tier) -> bool:
        """Whether the viewer is within the hourly quota. False – they have already been refused."""
        limit = _quota_per_hour(tier)
        if not limit:
            return True
        used = await count_bot_uses(user, KIND_GEMINI, Quota.WINDOW_MINUTES)
        if used < limit:
            return True
        # A slot frees up when the earliest request drops out of the window
        age = await oldest_bot_use_age(user, KIND_GEMINI, Quota.WINDOW_MINUTES) or 0
        minutes = max(1, math.ceil((Quota.WINDOW_MINUTES * 60 - age) / 60))
        await self._deny(message, user, 'quota_exceeded',
                         limit=limit, minutes=minutes, window=Quota.WINDOW_MINUTES)
        return False

    def _route(self, message: twitchio.ChatMessage):
        """Determine the command, the request text and whether the bot was addressed.

        Returns (entry, prompt, addressed), or (None, None, False) when the message is
        not for the bot. Addressing is a call word, an @mention or a reply, and it is
        required only for free text and for a command it precedes: a bare `!who ник`
        works without it. Args are taken as written, so `!who securityexpert` keeps the
        nick instead of losing the trigger inside it.
        """
        text = message.text.strip()
        lowered = text.lower()

        entry = self._registry.resolve(lowered)
        if entry is not None:
            return entry, lowered, False

        # The whole nick only: @botname_fan is somebody else
        mention = re.compile(re.escape(f'@{self.bot.bot_name}') + r'(?!\w)', re.IGNORECASE)
        is_mention = bool(mention.search(text))
        is_sosur = bool(SOSUR_RE.search(text))
        is_reply = reply_to_bot(message, self.bot.bot_id) is not None
        if not (is_mention or is_sosur or is_reply):
            return None, None, False

        prompt = mention.sub('', lowered)
        if not is_mention:
            # Addressed by word: that one word goes, the rest is the question – a nick
            # like securityexpert in it must reach the model
            prompt = SOSUR_RE.sub('', prompt, count=1)
        prompt = prompt.strip()
        if not prompt:
            prompt = lowered
        return self._registry.resolve(prompt), prompt, True

    @commands.Component.listener()
    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:
        # The cache may have remembered them as a non-follower a minute ago
        self._followers.forget(payload.user.id)
        await handle_follow(self.bot, payload)
