"""Конфигурация из окружения.

Здесь только .env: секреты, числа и флаги. Всё, что бот произносит,
живёт в CONTENT.md и читается через src/core/content.py.
"""
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# --- Разбор переменных окружения -------------------------------------------

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
    """Вероятность в процентах 0..100 → доля 0..1.

    Исторически CAPS_PROBABILITY задавалась долей (0.3). Такое значение
    распознаётся по точке в записи и принимается как есть.
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
    """Разброс интервала фонового цикла: PREFIX_MIN_MINUTES и PREFIX_MAX_MINUTES.

    Старое имя PREFIX_MINUTES (одно фиксированное число) ещё принимается и
    становится обеими границами – цикл ведёт себя как раньше, а в лог уходит
    подсказка, как включить разброс.
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
    # Токен владельца канала, а не бота: награды за баллы канала управляются
    # только от его имени, прав модератора для этого Twitch не даёт
    BROADCASTER_TOKEN: str | None = os.getenv('TWITCH_BROADCASTER_TOKEN')
    BROADCASTER_REFRESH: str | None = os.getenv('TWITCH_BROADCASTER_REFRESH')


class Gemini:
    API_KEY: str | None = os.getenv('GEMINI_API_KEY')
    MODEL: str = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    TEMPERATURE: float = _env_float('GEMINI_TEMPERATURE', 1.5, 0.0, 2.0)
    THINKING_BUDGET: int = _env_int('GEMINI_THINKING_BUDGET', 0, -1, 32768)
    CONCURRENCY: int = _env_int('GEMINI_CONCURRENCY', 5, 1, 50)
    TIMEOUT: int = _env_int('GEMINI_TIMEOUT', 60, 5, 600)
    RETRIES: int = _env_int('GEMINI_RETRIES', 2, 0, 5)

class Chat:
    MAX_CHUNKS: int = _env_int('CHAT_MAX_CHUNKS', 3, 1, 5)


class Caps:
    PROBABILITY: float = _env_percent('CAPS_PROBABILITY', 30)


class Cooldown:
    # Одна лесенка по статусу зрителя на все команды обоих классов.
    # Стример, модератор и подписчик не ждут никогда – их значения не
    # настраиваются, это правило, а не параметр.
    VIP: int = _env_int('COOLDOWN_VIP', 10, 0, 3600)
    REGULAR: int = _env_int('COOLDOWN_REGULAR', 30, 0, 3600)


class Quota:
    # Потолок обращений к Gemini за час – поверх кулдауна. Стример, модераторы
    # и подписчики не ограничены: у них и кулдауна нет. 0 – без лимита
    VIP_PER_HOUR: int = _env_int('QUOTA_VIP_PER_HOUR', 60, 0, 10_000)
    FOLLOWER_PER_HOUR: int = _env_int('QUOTA_FOLLOWER_PER_HOUR', 30, 0, 10_000)
    WINDOW_MINUTES: int = _env_int('QUOTA_WINDOW_MINUTES', 60, 1, 1440)


class Follow:
    # Без фолова бот не отвечает вовсе, кроме справки. Проверка идёт через
    # Helix и кэшируется: фолов меняется редко, а сообщений много
    REQUIRED: bool = _env_bool('FOLLOW_REQUIRED', True)
    CACHE_MINUTES: int = _env_int('FOLLOW_CACHE_MINUTES', 15, 1, 1440)
    # Как часто повторять одному человеку предложение зафоловиться
    HINT_MINUTES: int = _env_int('FOLLOW_HINT_MINUTES', 10, 1, 1440)


class Picture:
    # !ascii: картинка по ссылке рисуется символами Брайля. Ссылку даёт
    # зритель, поэтому ограничиваем и время, и размер
    ENABLED: bool = _env_bool('PICTURE_ENABLED', True)
    TIMEOUT: float = _env_float('PICTURE_TIMEOUT', 10.0, 1.0, 60.0)
    MAX_BYTES: int = _env_int('PICTURE_MAX_BYTES', 10 * 1024 * 1024, 1024, 100 * 1024 * 1024)
    # Ширина строки арта. Строка должна влезать в колонку чата целиком:
    # шире – порвётся переносом и картинка разъедется
    MAX_COLS: int = _env_int('PICTURE_MAX_COLS', 26, 14, 60)
    # Поправка на пропорции ячейки чата. Символ Брайля выше, чем вдвое
    # своей ширины, поэтому точка не квадратная и картинка без поправки
    # выглядит вытянутой по вертикали. Меньше 1 – сжать по высоте
    ASPECT: float = _env_float('PICTURE_ASPECT', 0.8, 0.3, 2.0)
    # Выедать заливку: сплошное пятно теряет форму, поэтому от него
    # остаются края. Тонкие линии не страдают – они сами края
    HOLLOW: bool = _env_bool('PICTURE_HOLLOW', True)
    # Насколько тёмные места остаются залитыми поверх контура – это и
    # даёт объём. Больше – больше заливки, 0 – только контур
    SHADOW: int = _env_int('PICTURE_SHADOW', 45, 0, 255)
    # Сколько картинок зритель может нарисовать за эфир. Команда открыта со
    # значка подписчика: фолловеру и не фолловеру она недоступна вовсе.
    # Стример не ограничен, 0 – без лимита
    PER_STREAM_VIP: int = _env_int('PICTURE_PER_STREAM_VIP', 3, 0, 1000)
    PER_STREAM_SUB: int = _env_int('PICTURE_PER_STREAM_SUB', 10, 0, 1000)
    # Показывать картинку Gemini, чтобы он решил, можно ли её рисовать.
    # Выключать не стоит: ссылку приносит зритель
    CHECK: bool = _env_bool('PICTURE_CHECK', True)


class Stream:
    # Сессия бота – это эфир. Если эфир оборвался и снова пошёл в течение
    # RESUME_MINUTES, это тот же эфир: роллы, щиты и проклятия продолжаются.
    # 0 – любой новый эфир начинает новую сессию
    RESUME_MINUTES: int = _env_int('STREAM_RESUME_MINUTES', 15, 0, 240)


class Roll:
    MIN: int = _env_int('ROLL_MIN', 1, 1, 1000)
    MAX: int = _env_int('ROLL_MAX', 100, 2, 1000)
    # Бесплатных !roll за сессию – по статусу зрителя. Дальше только награда
    # за баллы. Стример катает без лимита, не фолловер не катает вовсе
    FREE_PER_SESSION: int = _env_int('ROLL_FREE_PER_SESSION', 3, 1, 1000)
    FREE_VIP: int = _env_int('ROLL_FREE_VIP', 5, 1, 1000)
    FREE_SUB: int = _env_int('ROLL_FREE_SUB', 10, 1, 1000)
    # Итоги прошлого эфира: китежанину – щит от перебросов, залупе – проклятие.
    # Отсчёт PERK_MINUTES идёт с первого появления человека в чате нового эфира
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
    # Награды за баллы канала. Работают, только если есть токен канала –
    # без него флаг ничего не включает, а бот пишет в лог ссылку на авторизацию
    ENABLED: bool = _env_bool('REWARDS_ENABLED', True)
    # Цены в баллах. Зритель без подписки зарабатывает около 300 в час
    COST_EXTRA: int = _env_int('REWARD_COST_EXTRA', 100, 1, 1_000_000)
    COST_REROLL: int = _env_int('REWARD_COST_REROLL', 250, 1, 1_000_000)
    COST_CURSE: int = _env_int('REWARD_COST_CURSE', 1000, 1, 1_000_000)
    COST_SHIELD: int = _env_int('REWARD_COST_SHIELD', 500, 1, 1_000_000)
    # Проклятие: бросок жертвы не выше CURSE_CEILING, каждый следующий бросок
    # по ней – свой или чужой переброс – опускает потолок на CURSE_STEP, но не ниже CURSE_FLOOR.
    # На дне потолок держится CURSE_HOLD_MINUTES, потом проклятие спадает
    CURSE_CEILING, CURSE_FLOOR = _curse_range()
    CURSE_STEP: int = _env_int('REWARD_CURSE_STEP', 5, 0, 1000)
    CURSE_HOLD_MINUTES: int = _env_int('REWARD_CURSE_HOLD_MINUTES', 15, 1, 1440)
    # После успешного переброса цель столько минут неуязвима для новых перебросов:
    # лимит Twitch считает каждого атакующего отдельно, и толпа добивает одну цель.
    # Проклятие защиту пробивает, как и щит. 0 – защиты нет
    REROLL_PROTECT_MINUTES: int = _env_int('REWARD_REROLL_PROTECT_MINUTES', 3, 0, 1440)
    # Сколько раз один зритель может перебросить и проклясть за эфир – лимит
    # у каждой награды свой, считает его сам Twitch. 0 – без лимита
    ATTACK_MAX_PER_USER: int = _env_int('REWARD_ATTACK_MAX_PER_USER', 3, 0, 1000)


class Context:
    CHAT_MESSAGES: int = _env_int('CONTEXT_CHAT_MESSAGES', 50, 1, 1000)
    SEARCH_RESULTS: int = _env_int('CONTEXT_SEARCH_RESULTS', 10, 0, 100)
    SEARCH_KNOWLEDGE_SHARE: float = _env_percent('CONTEXT_SEARCH_KNOWLEDGE_SHARE', 50)
    KNOWLEDGE_RANDOM: int = _env_int('CONTEXT_KNOWLEDGE_RANDOM', 10, 0, 100)
    WHO_MESSAGES: int = _env_int('CONTEXT_WHO_MESSAGES', 30, 1, 500)
    VERSUS_MESSAGES: int = _env_int('CONTEXT_VERSUS_MESSAGES', 30, 1, 500)
    SUMMARY_MESSAGES: int = _env_int('CONTEXT_SUMMARY_MESSAGES', 500, 10, 5000)
    USER_INTERACTIONS: int = _env_int('CONTEXT_USER_INTERACTIONS', 10, 0, 100)


class Help:
    # Напоминание в чат о командах бота. Шлётся только пока идёт эфир и только
    # если в чате кто-то писал: пустому чату напоминать не о чем.
    # Интервал ровный: это справка, её ждут предсказуемо
    ANNOUNCE_ENABLED: bool = _env_bool('HELP_ANNOUNCE_ENABLED', True)
    ANNOUNCE_INTERVAL_MINUTES: int = _env_int('HELP_ANNOUNCE_INTERVAL_MINUTES', 15, 1, 1440)


class Proactive:
    ENABLED: bool = _env_bool('PROACTIVE_ENABLED', True)
    # Пауза до следующей реплики берётся случайно из этого диапазона
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
