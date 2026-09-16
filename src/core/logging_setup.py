import logging
import logging.handlers
from pathlib import Path

from src.core.config import Logging

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

_configured = False


def setup_logging(default_level: str = 'INFO') -> None:
    """Настроить корневой логгер.

    Уровень берётся из LOG_LEVEL, иначе используется default_level
    (INFO для бота, WARNING для CLI-команд). При заданном LOG_FILE
    дополнительно пишется ротируемый файл.
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

    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)
    _configured = True
