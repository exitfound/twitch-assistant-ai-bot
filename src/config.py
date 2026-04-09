import logging
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / 'prompt.txt'
EMOTES_PATH = Path(__file__).resolve().parent.parent / 'emotes.txt'

_prompt_cache: str | None = None
_prompt_mtime: float = 0.0

_emotes_cache: list[str] | None = None
_emotes_mtime: float = 0.0


def _load_emotes_sync() -> list[str]:
    global _emotes_cache, _emotes_mtime
    try:
        mtime = EMOTES_PATH.stat().st_mtime
    except FileNotFoundError:
        logger.warning('emotes.txt not found at %s', EMOTES_PATH)
        return _emotes_cache or []
    if _emotes_cache is None or mtime != _emotes_mtime:
        lines = EMOTES_PATH.read_text(encoding='utf-8').splitlines()
        _emotes_cache = [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]
        _emotes_mtime = mtime
    return _emotes_cache


def _load_prompt_sync() -> str:
    global _prompt_cache, _prompt_mtime
    try:
        mtime = PROMPT_PATH.stat().st_mtime
    except FileNotFoundError:
        logger.error('prompt.txt not found at %s', PROMPT_PATH)
        return _prompt_cache or ''
    if _prompt_cache is None or mtime != _prompt_mtime:
        _prompt_cache = PROMPT_PATH.read_text(encoding='utf-8').strip()
        _prompt_mtime = mtime
    return _prompt_cache


def validate_config() -> None:
    missing = []
    for var in ('TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET', 'TWITCH_BOT_ID', 'TWITCH_CHANNEL', 'GEMINI_API_KEY'):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        raise EnvironmentError(f'Missing required env vars: {", ".join(missing)}')


class Twitch:
    CLIENT_ID: str | None = os.getenv('TWITCH_CLIENT_ID')
    CLIENT_SECRET: str | None = os.getenv('TWITCH_CLIENT_SECRET')
    BOT_ID: str | None = os.getenv('TWITCH_BOT_ID')
    CHANNEL: str | None = os.getenv('TWITCH_CHANNEL')
    BOT_TOKEN: str | None = os.getenv('TWITCH_BOT_TOKEN')
    BOT_REFRESH: str | None = os.getenv('TWITCH_BOT_REFRESH')


class Gemini:
    API_KEY: str | None = os.getenv('GEMINI_API_KEY')
    MODEL: str = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    TEMPERATURE: float = float(os.getenv('GEMINI_TEMPERATURE', '1.5'))
    THINKING_BUDGET: int = int(os.getenv('GEMINI_THINKING_BUDGET', '0'))

    @staticmethod
    def get_system_instruction() -> str:
        return _load_prompt_sync()


class Caps:
    PROBABILITY: float = float(os.getenv('CAPS_PROBABILITY', '0.3'))


class Cooldown:
    SECONDS: int = int(os.getenv('COOLDOWN_SECONDS', '10'))
    COMMAND_SECONDS: int = int(os.getenv('COOLDOWN_COMMAND_SECONDS', '30'))
    MESSAGE: str = os.getenv('COOLDOWN_MESSAGE', 'гуляй, отвечу повторно через {seconds} сек.')


class Context:
    CHAT_MESSAGES: int = int(os.getenv('CONTEXT_CHAT_MESSAGES', '50'))
    SEARCH_RESULTS: int = int(os.getenv('CONTEXT_SEARCH_RESULTS', '10'))
    KNOWLEDGE_RANDOM: int = int(os.getenv('CONTEXT_KNOWLEDGE_RANDOM', '10'))
    WHO_MESSAGES: int = int(os.getenv('CONTEXT_WHO_MESSAGES', '30'))
    VERSUS_MESSAGES: int = int(os.getenv('CONTEXT_VERSUS_MESSAGES', '30'))


class Proactive:
    INTERVAL_MINUTES: int = int(os.getenv('PROACTIVE_INTERVAL_MINUTES', '15'))
    ENABLED: bool = os.getenv('PROACTIVE_ENABLED', 'true').lower() in ('true', '1', 'yes')


def _parse_emote_probability() -> float:
    raw = os.getenv('EMOTE_PROBABILITY', '10')
    try:
        value = int(raw)
        if not 0 <= value <= 100:
            raise ValueError(f'значение вне диапазона: {value}')
        return value / 100.0
    except (ValueError, TypeError) as e:
        logger.warning('Некорректное EMOTE_PROBABILITY=%r (%s), используется 10%%', raw, e)
        return 0.1


class Emote:
    PROBABILITY: float = _parse_emote_probability()
    SPAM_ENABLED: bool = os.getenv('EMOTE_SPAM_ENABLED', 'false').lower() in ('true', '1', 'yes')
    SPAM_INTERVAL_MINUTES: int = int(os.getenv('EMOTE_SPAM_INTERVAL_MINUTES', '10'))

    @staticmethod
    def get_list() -> list[str]:
        return _load_emotes_sync()


class Roll:
    LOSER_SELF: str = os.getenv(
        'ROLL_LOSER_SELF',
        '@{user} выбил {value} из 100 — ЗАЛУПА НЕПРИЧЕСАННАЯ ЭТОГО СТРИМА!',
    )
    LOSER_OTHER: str = os.getenv(
        'ROLL_LOSER_OTHER',
        '@{user} выбил {value} из 100. Залупа стрима: @{loser} ({loser_val} из 100)',
    )
    INFO_LOSER: str = os.getenv(
        'ROLL_INFO_LOSER',
        'Залупа стрима этой сессии: @{loser} ({loser_val} из 100)',
    )
    INFO_NO_ROLLS: str = os.getenv(
        'ROLL_INFO_NO_ROLLS',
        'В этой сессии ещё никто не катал.',
    )
