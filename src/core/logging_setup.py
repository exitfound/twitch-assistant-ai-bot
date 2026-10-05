import logging
import logging.handlers
import re
from datetime import datetime
from pathlib import Path

from src.core.config import Clock, Logging

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

_configured = False

# twitchio puts request URLs and tokens into its error texts: a refused refresh carries the
# client secret and the refresh token, and docker logs keep them on disk
_SECRET_RE = re.compile(
    r"""((?:client_secret|refresh_token|access_token|refresh|token)(?:=|': '|": "|: ")|[?&]code=)[^&\s'"]+"""
)


def redact(text: str) -> str:
    """The text with every secret value replaced by ***."""
    return _SECRET_RE.sub(r'\1***', text)


class _RedactingFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def make_formatter() -> logging.Formatter:
    """LOG_FORMAT with times in BOT_TIMEZONE and secrets hidden: the container runs in UTC,
    and the log must match the times in chat and in session ids."""
    formatter = _RedactingFormatter(LOG_FORMAT)
    formatter.converter = lambda seconds: datetime.fromtimestamp(seconds, Clock.ZONE).timetuple()
    return formatter


def setup_logging(default_level: str = 'INFO') -> None:
    """Configure the root logger.

    The level comes from LOG_LEVEL, otherwise default_level is used
    (INFO for the bot, WARNING for CLI commands). When LOG_FILE is set,
    a rotating file is written as well.
    """
    global _configured
    if _configured:
        return

    level_name = (Logging.LEVEL or default_level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
        logging.getLogger(__name__).warning(
            'Неизвестный LOG_LEVEL=%r, используется INFO', Logging.LEVEL
        )

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if Logging.FILE:
        try:
            path = Path(Logging.FILE).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                path,
                maxBytes=Logging.FILE_MAX_BYTES,
                backupCount=Logging.FILE_BACKUPS,
                encoding='utf-8',
            ))
        except OSError:
            logging.getLogger(__name__).exception('Не удалось открыть LOG_FILE=%s', Logging.FILE)

    formatter = make_formatter()
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    _configured = True
