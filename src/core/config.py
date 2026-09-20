"""Configuration from the environment.

Only .env lives here: secrets, numbers and flags. Everything the bot says
lives in CONTENT.md and is read through src/core/content.py.
"""
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# --- Parsing environment variables -----------------------------------------

def _env_raw(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = _env_raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning('Некорректное %s=%r (ожидалось целое), используется %s', name, raw, default)
        return default
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        logger.warning('%s=%s вне диапазона [%s, %s], используется %s', name, value, lo, hi, default)
        return default
    return value


def _env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = _env_raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning('Некорректное %s=%r (ожидалось число), используется %s', name, raw, default)
        return default
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        logger.warning('%s=%s вне диапазона [%s, %s], используется %s', name, value, lo, hi, default)
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_raw(name)
    if raw is None:
        return default
    return raw.lower() in ('true', '1', 'yes', 'on')


def _env_percent(name: str, default_percent: float) -> float:
    """Probability as a percentage 0..100 → fraction 0..1.

    Historically CAPS_PROBABILITY was given as a fraction (0.3). Such a value
    is recognised by the dot in it and accepted as is.
    """
    raw = _env_raw(name)
    if raw is None:
        return default_percent / 100.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning('Некорректное %s=%r (ожидалось 0..100), используется %s%%', name, raw, default_percent)
        return default_percent / 100.0
    if 0.0 <= value <= 1.0 and '.' in raw:
        logger.warning(
            '%s=%s задано долей – трактую как %.0f%%. Новый формат – проценты, например %s=%.0f',
            name, raw, value * 100, name, value * 100,
        )
        return value
    if 0.0 <= value <= 100.0:
        return value / 100.0
    logger.warning('%s=%s вне диапазона 0..100, используется %s%%', name, raw, default_percent)
    return default_percent / 100.0


def _interval_range(prefix: str, default_min: int, default_max: int) -> tuple[int, int]:
    """Interval range of a background loop: PREFIX_MIN_MINUTES and PREFIX_MAX_MINUTES.

    The old name PREFIX_MINUTES (one fixed number) is still accepted and
    becomes both bounds – the loop behaves as before, and the log gets
    a hint on how to enable the spread.
    """
    legacy = _env_int(f'{prefix}_MINUTES', 0, 0, 1440)
    if legacy:
        logger.warning(
            '%s_MINUTES задаёт ровный интервал. Для разброса укажи %s_MIN_MINUTES и %s_MAX_MINUTES',
            prefix, prefix, prefix,
        )
        default_min = default_max = legacy
    low = _env_int(f'{prefix}_MIN_MINUTES', default_min, 1, 1440)
    high = _env_int(f'{prefix}_MAX_MINUTES', default_max, 1, 1440)
    if low > high:
        logger.warning('%s_MIN_MINUTES=%s больше %s_MAX_MINUTES=%s, значения переставлены', prefix, low, prefix, high)
        low, high = high, low
    return low, high


def validate_config() -> None:
    missing = []
    for var in ('TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET', 'TWITCH_BOT_ID', 'TWITCH_CHANNEL', 'GEMINI_API_KEY'):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        raise EnvironmentError(f'Missing required env vars: {", ".join(missing)}')


class Logging:
    LEVEL: str | None = _env_raw('LOG_LEVEL')
    FILE: str | None = _env_raw('LOG_FILE')
    FILE_MAX_BYTES: int = _env_int('LOG_FILE_MAX_BYTES', 5_000_000, 10_000, 1_000_000_000)
    FILE_BACKUPS: int = _env_int('LOG_FILE_BACKUPS', 3, 0, 50)


class Twitch:
    CLIENT_ID: str | None = os.getenv('TWITCH_CLIENT_ID')
    CLIENT_SECRET: str | None = os.getenv('TWITCH_CLIENT_SECRET')
    BOT_ID: str | None = os.getenv('TWITCH_BOT_ID')
    CHANNEL: str | None = os.getenv('TWITCH_CHANNEL')
    BOT_TOKEN: str | None = os.getenv('TWITCH_BOT_TOKEN')
    BOT_REFRESH: str | None = os.getenv('TWITCH_BOT_REFRESH')
    # The channel owner's token, not the bot's: channel-points rewards can be managed
    # only on the owner's behalf, Twitch does not grant this to moderators
    BROADCASTER_TOKEN: str | None = os.getenv('TWITCH_BROADCASTER_TOKEN')
    BROADCASTER_REFRESH: str | None = os.getenv('TWITCH_BROADCASTER_REFRESH')


class Gemini:
    API_KEY: str | None = os.getenv('GEMINI_API_KEY')
    MODEL: str = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    TEMPERATURE: float = _env_float('GEMINI_TEMPERATURE', 1.5, 0.0, 2.0)
    # !ask – factual mode: at the chatter temperature the model makes things up more often
    ASK_TEMPERATURE: float = _env_float('GEMINI_ASK_TEMPERATURE', 0.5, 0.0, 2.0)
    THINKING_BUDGET: int = _env_int('GEMINI_THINKING_BUDGET', 0, -1, 32768)
    CONCURRENCY: int = _env_int('GEMINI_CONCURRENCY', 5, 1, 50)
    TIMEOUT: int = _env_int('GEMINI_TIMEOUT', 60, 5, 600)
    RETRIES: int = _env_int('GEMINI_RETRIES', 2, 0, 5)

class Chat:
    # !summary's message cap; 2 like !ask and !versus (owner, 2026-09-19)
    MAX_CHUNKS: int = _env_int('CHAT_MAX_CHUNKS', 2, 1, 5)


class Caps:
    PROBABILITY: float = _env_percent('CAPS_PROBABILITY', 30)


class Cooldown:
    # One ladder by viewer status for all commands of both classes.
    # Broadcaster, moderator and subscriber never wait – their values are not
    # configurable, this is a rule, not a parameter.
    VIP: int = _env_int('COOLDOWN_VIP', 10, 0, 3600)
    REGULAR: int = _env_int('COOLDOWN_REGULAR', 30, 0, 3600)


class Quota:
    # Cap on Gemini requests per hour – on top of the cooldown. Broadcaster, moderators
    # and subscribers are unlimited: they have no cooldown either. 0 – no limit
    VIP_PER_HOUR: int = _env_int('QUOTA_VIP_PER_HOUR', 60, 0, 10_000)
    FOLLOWER_PER_HOUR: int = _env_int('QUOTA_FOLLOWER_PER_HOUR', 30, 0, 10_000)
    WINDOW_MINUTES: int = _env_int('QUOTA_WINDOW_MINUTES', 60, 1, 1440)
    # The bill's emergency brake: requests of everyone together per window. The
    # per-viewer quota does not bound spend, because every badge above follower is
    # unlimited. The broadcaster is not counted against it – that spend is deliberate.
    # 0 – no limit. The owner set 200 on 2026-09-20: the busiest hour on record is
    # 14 served requests, so it is an emergency brake, not a working limit
    CHANNEL_PER_HOUR: int = _env_int('QUOTA_CHANNEL_PER_HOUR', 200, 0, 100_000)


class Follow:
    # Without a follow the bot does not answer at all, except for help. The check goes
    # through Helix and is cached: follows change rarely, messages are many
    REQUIRED: bool = _env_bool('FOLLOW_REQUIRED', True)
    CACHE_MINUTES: int = _env_int('FOLLOW_CACHE_MINUTES', 15, 1, 1440)
    # How often to repeat the follow suggestion to the same person
    HINT_MINUTES: int = _env_int('FOLLOW_HINT_MINUTES', 10, 1, 1440)


class Summary:
    # !summary per stream (offline – per 24 hours), the current and the previous
    # stream counted together (owner, 2026-09-20). The broadcaster is not limited.
    # 0 – no limit
    PER_STREAM_FOLLOWER: int = _env_int('SUMMARY_PER_STREAM_FOLLOWER', 1, 0, 1000)
    PER_STREAM_VIP: int = _env_int('SUMMARY_PER_STREAM_VIP', 3, 0, 1000)
    PER_STREAM_SUB: int = _env_int('SUMMARY_PER_STREAM_SUB', 10, 0, 1000)


class Who:
    # !who and !versus per stream (offline – per 24 hours), each command counted
    # on its own; the same numbers as !summary (owner, 2026-09-20). The broadcaster
    # is not limited. 0 – no limit
    PER_STREAM_FOLLOWER: int = _env_int('WHO_PER_STREAM_FOLLOWER', 1, 0, 1000)
    PER_STREAM_VIP: int = _env_int('WHO_PER_STREAM_VIP', 3, 0, 1000)
    PER_STREAM_SUB: int = _env_int('WHO_PER_STREAM_SUB', 10, 0, 1000)


class Picture:
    # !ascii: a picture from a link is drawn in braille characters. The link comes
    # from a viewer, so both time and size are limited
    ENABLED: bool = _env_bool('PICTURE_ENABLED', True)
    TIMEOUT: float = _env_float('PICTURE_TIMEOUT', 10.0, 1.0, 60.0)
    MAX_BYTES: int = _env_int('PICTURE_MAX_BYTES', 10 * 1024 * 1024, 1024, 100 * 1024 * 1024)
    # Width of an art line. A line must fit into the chat column whole:
    # wider, and wrapping breaks it and the picture falls apart
    MAX_COLS: int = _env_int('PICTURE_MAX_COLS', 26, 14, 60)
    # Correction for the chat cell proportions. A braille character is taller than
    # twice its width, so a dot is not square and a picture without correction
    # looks stretched vertically. Below 1 – squeeze vertically
    ASPECT: float = _env_float('PICTURE_ASPECT', 0.8, 0.3, 2.0)
    # Hollow out the fill: a solid blob loses its shape, so only its
    # edges remain. Thin lines do not suffer – they are edges themselves
    HOLLOW: bool = _env_bool('PICTURE_HOLLOW', True)
    # How dark a place must be to stay filled on top of the contour – that is what
    # gives volume. Higher – more fill, 0 – contour only
    SHADOW: int = _env_int('PICTURE_SHADOW', 45, 0, 255)
    # How many pictures a viewer may draw per stream. The command is open from the
    # subscriber badge up: followers and non-followers cannot use it at all.
    # The broadcaster is unlimited, 0 – no limit
    PER_STREAM_VIP: int = _env_int('PICTURE_PER_STREAM_VIP', 3, 0, 1000)
    PER_STREAM_SUB: int = _env_int('PICTURE_PER_STREAM_SUB', 10, 0, 1000)
    # Show the picture to Gemini so it decides whether it may be drawn.
    # Better not to turn off: the link comes from a viewer
    CHECK: bool = _env_bool('PICTURE_CHECK', True)


class Stream:
    # The bot session is the stream. If the stream dropped and came back within
    # RESUME_MINUTES, it is the same stream: rolls, shields and curses carry on.
    # 0 – every new stream starts a new session
    RESUME_MINUTES: int = _env_int('STREAM_RESUME_MINUTES', 15, 0, 240)


class Roll:
    MIN: int = _env_int('ROLL_MIN', 1, 1, 1000)
    MAX: int = _env_int('ROLL_MAX', 100, 2, 1000)
    # Free !roll throws per session – by viewer status. Beyond that only the
    # channel-points reward. The broadcaster rolls without limit, a non-follower not at all
    FREE_PER_SESSION: int = _env_int('ROLL_FREE_PER_SESSION', 3, 1, 1000)
    FREE_VIP: int = _env_int('ROLL_FREE_VIP', 5, 1, 1000)
    FREE_SUB: int = _env_int('ROLL_FREE_SUB', 10, 1, 1000)
    # Results of the previous stream: the «китежанин» gets a shield against rerolls,
    # the «залупа» gets a curse. PERK_MINUTES counts from the person's first
    # appearance in the chat of the new stream
    PERKS_ENABLED: bool = _env_bool('ROLL_PERKS_ENABLED', True)
    PERK_MINUTES: int = _env_int('ROLL_PERK_MINUTES', 30, 1, 600)


def _curse_range() -> tuple[int, int]:
    ceiling = _env_int('REWARD_CURSE_CEILING', 75, 1, 1000)
    floor = _env_int('REWARD_CURSE_FLOOR', 25, 1, 1000)
    if floor > ceiling:
        logger.warning('REWARD_CURSE_FLOOR=%s больше REWARD_CURSE_CEILING=%s, значения переставлены', floor, ceiling)
        ceiling, floor = floor, ceiling
    return ceiling, floor


class Rewards:
    # Channel-points rewards. They work only when the channel token is present –
    # without it the flag enables nothing, and the bot logs an authorization link
    ENABLED: bool = _env_bool('REWARDS_ENABLED', True)
    # Prices in points. A viewer without a subscription earns about 300 per hour
    COST_EXTRA: int = _env_int('REWARD_COST_EXTRA', 100, 1, 1_000_000)
    COST_REROLL: int = _env_int('REWARD_COST_REROLL', 250, 1, 1_000_000)
    COST_CURSE: int = _env_int('REWARD_COST_CURSE', 1000, 1, 1_000_000)
    COST_SHIELD: int = _env_int('REWARD_COST_SHIELD', 500, 1, 1_000_000)
    # Curse: the victim's throw is at most CURSE_CEILING, every further throw
    # on them – their own or someone's reroll – lowers the ceiling by CURSE_STEP, not below CURSE_FLOOR.
    # On the floor the ceiling holds for CURSE_HOLD_MINUTES, then the curse lifts
    CURSE_CEILING, CURSE_FLOOR = _curse_range()
    CURSE_STEP: int = _env_int('REWARD_CURSE_STEP', 5, 0, 1000)
    CURSE_HOLD_MINUTES: int = _env_int('REWARD_CURSE_HOLD_MINUTES', 15, 1, 1440)
    # After a successful reroll the target is immune to new rerolls for this many minutes:
    # Twitch's limit counts each attacker separately, and a crowd finishes off one target.
    # A curse pierces this protection, like the shield. 0 – no protection
    REROLL_PROTECT_MINUTES: int = _env_int('REWARD_REROLL_PROTECT_MINUTES', 3, 0, 1440)
    # How many times one viewer may reroll and curse per stream – each reward
    # has its own limit, counted by Twitch itself. 0 – no limit
    ATTACK_MAX_PER_USER: int = _env_int('REWARD_ATTACK_MAX_PER_USER', 3, 0, 1000)


class Context:
    CHAT_MESSAGES: int = _env_int('CONTEXT_CHAT_MESSAGES', 50, 1, 1000)
    # A free-text answer during a stream gets the whole current stream and the whole
    # previous one (owner, 2026-09-19); this only caps a runaway stream. The longest
    # stream so far had 861 messages
    STREAM_MAX_MESSAGES: int = _env_int('CONTEXT_STREAM_MAX_MESSAGES', 2000, 100, 20_000)
    SEARCH_RESULTS: int = _env_int('CONTEXT_SEARCH_RESULTS', 10, 0, 100)
    SEARCH_KNOWLEDGE_SHARE: float = _env_percent('CONTEXT_SEARCH_KNOWLEDGE_SHARE', 50)
    KNOWLEDGE_RANDOM: int = _env_int('CONTEXT_KNOWLEDGE_RANDOM', 10, 0, 100)
    # !who (src/gemini/who.py): a fresh random sample of the target's whole history on
    # every call. WHO_MESSAGES – their last messages, the old context and the last
    # rung; WHO_RECENT_MESSAGES of them go into the sample as «what now»
    WHO_MESSAGES: int = _env_int('CONTEXT_WHO_MESSAGES', 30, 1, 500)
    WHO_EVENTS: int = _env_int('CONTEXT_WHO_EVENTS', 15, 0, 200)
    WHO_SAMPLE_MESSAGES: int = _env_int('CONTEXT_WHO_SAMPLE_MESSAGES', 40, 0, 500)
    WHO_RECENT_MESSAGES: int = _env_int('CONTEXT_WHO_RECENT_MESSAGES', 10, 0, 500)
    WHO_RELATIONS: int = _env_int('CONTEXT_WHO_RELATIONS', 2, 0, 20)
    VERSUS_MESSAGES: int = _env_int('CONTEXT_VERSUS_MESSAGES', 30, 1, 500)
    SUMMARY_MESSAGES: int = _env_int('CONTEXT_SUMMARY_MESSAGES', 500, 10, 5000)
    USER_INTERACTIONS: int = _env_int('CONTEXT_USER_INTERACTIONS', 10, 0, 100)


class Memory:
    # The bot's long-term memory of chatters: a chronicle of every finished
    # conversation and a profile of every active chatter, both written by Gemini.
    # See src/gemini/memory/
    ENABLED: bool = _env_bool('MEMORY_ENABLED', True)
    # A conversation is chat between two silences this long. Chat on this channel
    # happens only on streams, so it is a stream in practice, told by the messages
    # alone: stream events, restarts and missed stream ends cannot shift it. Measured
    # 2026-09-19 over the whole history: 71 silences over 6 hours, just one between
    # 3 and 6 – the chat splits cleanly at 3 hours
    SILENCE_MINUTES: int = _env_int('MEMORY_SILENCE_MINUTES', 180, 30, 24 * 60)
    # A conversation with fewer chat messages gets no chronicle and updates no profiles
    CONVERSATION_MIN_MESSAGES: int = _env_int('MEMORY_CONVERSATION_MIN_MESSAGES', 50, 1, 100_000)
    # A chatter gets a profile (or an update) for a conversation they wrote this much in
    PROFILE_MIN_MESSAGES: int = _env_int('MEMORY_PROFILE_MIN_MESSAGES', 10, 1, 10_000)


class Help:
    # Chat reminder of the bot's commands. Sent only while the stream is live and only
    # if somebody has written in chat: an empty chat has nothing to be reminded of.
    # The interval is fixed: this is help, people expect it predictably
    ANNOUNCE_ENABLED: bool = _env_bool('HELP_ANNOUNCE_ENABLED', True)
    ANNOUNCE_INTERVAL_MINUTES: int = _env_int('HELP_ANNOUNCE_INTERVAL_MINUTES', 30, 1, 1440)


class Proactive:
    ENABLED: bool = _env_bool('PROACTIVE_ENABLED', True)
    # The pause before the next remark is picked at random from this range
    INTERVAL_MIN_MINUTES, INTERVAL_MAX_MINUTES = _interval_range('PROACTIVE_INTERVAL', 5, 30)
    ACTIVE_WINDOW: int = _env_int('PROACTIVE_ACTIVE_WINDOW', 20, 1, 200)
    TARGET_PROBABILITY: float = _env_percent('PROACTIVE_TARGET_PROBABILITY', 50)


def _emote_spam_range() -> tuple[int, int]:
    low = _env_int('EMOTE_SPAM_MIN', 1, 1, 20)
    high = _env_int('EMOTE_SPAM_MAX', 5, 1, 20)
    if low > high:
        logger.warning('EMOTE_SPAM_MIN=%s больше EMOTE_SPAM_MAX=%s, значения переставлены', low, high)
        low, high = high, low
    return low, high


class Emote:
    PROBABILITY: float = _env_percent('EMOTE_PROBABILITY', 10)
    SPAM_ENABLED: bool = _env_bool('EMOTE_SPAM_ENABLED', False)
    SPAM_INTERVAL_MIN_MINUTES, SPAM_INTERVAL_MAX_MINUTES = _interval_range('EMOTE_SPAM_INTERVAL', 5, 20)
    SPAM_MIN, SPAM_MAX = _emote_spam_range()
