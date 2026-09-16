"""Тексты бота: один CONTENT.md с горячей перезагрузкой по mtime.

Разделение ответственности: src/core/config.py читает окружение (секреты, числа,
флаги), этот модуль — всё, что бот произносит. Промпты, заголовки секций
контекста, ответы в чат, списки эмотов и фолов лежат в одном файле и
правятся без перезапуска.

Формат — Markdown, чтобы русская проза лежала без кавычек и экранирования:

    ## секция
    ### ключ
    значение до следующего заголовка

Всё до первого `###` внутри секции — примечания, они игнорируются, как и
комментарии `<!-- ... -->`, в том числе многострочные.

Синтаксически сломать такой файл почти нельзя, поэтому вместо разбора
формата проверяется смысл: дубли ключей и незакрытые секции пишутся в лог,
а отсутствующие и лишние ключи ловятся на старте через validate_content().
"""
import logging
import re
from pathlib import Path

from src.core.utils import safe_format

logger = logging.getLogger(__name__)

# Корень проекта: src/core/content.py → на три уровня вверх
CONTENT_PATH = Path(__file__).resolve().parents[2] / 'CONTENT.md'

SECTION_RE = re.compile(r'^##\s+(\S+)\s*$')
KEY_RE = re.compile(r'^###\s+(\S+)\s*$')

# Ключи, без которых бот работать не может — проверяются при запуске.
REQUIRED = {
    'prompts': (
        'system', 'ask', 'summary', 'summary_request', 'who', 'versus',
        'proactive_user', 'proactive_general',
        'user_question', 'interaction_line',
    ),
    'labels': (
        'facts', 'chat', 'channel', 'language',
        'user_facts', 'user_messages', 'user_interactions',
    ),
    'texts': (
        'help', 'stats', 'cooldown_local', 'cooldown_gemini', 'role_denied',
        'roll_loser_self', 'roll_loser_other', 'roll_free_left', 'roll_champion',
        'roll_cursed_self', 'roll_cursed_other',
        'roll_curse_step', 'roll_curse_hold', 'roll_curse_lifted',
        'roll_no_free', 'roll_no_free_reward', 'roll_error', 'roll_offline',
        'roll_perks_both', 'roll_perks_champion', 'roll_perks_loser',
        'roll_perk_shield_on', 'roll_perk_curse_on',
        'reward_extra_title', 'reward_reroll_title', 'reward_reroll_prompt',
        'reward_curse_title', 'reward_curse_prompt', 'reward_shield_title',
        'reward_extra_done', 'reward_extra_cursed', 'reward_reroll_done', 'reward_reroll_first', 'reward_curse_hit', 'reward_shield_done',
        'reward_refund_free_left', 'reward_refund_bad_target', 'reward_refund_self',
        'reward_refund_not_rolled', 'reward_refund_shielded', 'reward_refund_shield_active',
        'reward_refund_already_cursed', 'reward_refund_protected', 'reward_refund_unknown_target',
        'reward_refund_perk_shield', 'reward_refund_offline',
        'reward_error',
        'fact_usage', 'fact_saved',
        'defact_usage', 'defact_missing', 'defact_ambiguous', 'defact_done',
        'ask_usage', 'ask_error',
        'summary_empty', 'summary_error',
        'who_usage', 'who_unknown', 'who_failed',
        'versus_usage', 'versus_unknown', 'versus_failed',
        'no_answer', 'filtered', 'gen_error', 'gen_failed',
    ),
    'lists': ('emotes', 'follow', 'banned'),
}


def parse(raw: str) -> dict[str, dict[str, str]]:
    """Разобрать CONTENT.md в {секция: {ключ: значение}}.

    Дубли ключей логируются; побеждает последнее определение — при правке
    обычно дописывают ниже.
    """
    data: dict[str, dict[str, str]] = {}
    section: str | None = None
    key: str | None = None
    buffer: list[str] = []
    in_comment = False

    def flush() -> None:
        nonlocal key, buffer
        if section is not None and key is not None:
            if key in data[section]:
                logger.error('CONTENT.md: ключ %s.%s объявлен дважды, беру последний', section, key)
            data[section][key] = '\n'.join(buffer).strip()
        key, buffer = None, []

    for line in raw.splitlines():
        # Примечания <!-- ... -->, в том числе многострочные, в значения не попадают
        stripped = line.strip()
        if in_comment:
            in_comment = '-->' not in stripped
            continue
        if stripped.startswith('<!--'):
            in_comment = '-->' not in stripped
            continue
        match = SECTION_RE.match(line)
        if match:
            flush()
            section = match.group(1)
            data.setdefault(section, {})
            continue
        match = KEY_RE.match(line)
        if match:
            flush()
            if section is None:
                logger.error('CONTENT.md: ключ %s вне секции, пропущен', match.group(1))
                continue
            key = match.group(1)
            continue
        if key is not None:
            buffer.append(line)
    flush()
    return data


class _ContentFile:
    """CONTENT.md с перечитыванием при изменении mtime."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, str]] = {}
        self._mtime = 0.0
        self._error_logged = False

    def get(self) -> dict[str, dict[str, str]]:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._log_once('Файл не найден: %s', self._path)
            return self._data
        if self._data and mtime == self._mtime:
            return self._data
        try:
            raw = self._path.read_text(encoding='utf-8')
        except OSError as e:
            self._mtime = mtime
            self._log_once('Не удалось прочитать %s: %s — остаюсь на прошлой версии', self._path, e)
            return self._data
        data = parse(raw)
        if not data:
            # Пустой разбор — скорее всего снесли заголовки; прошлая версия лучше.
            self._mtime = mtime
            self._log_once('%s не содержит секций — остаюсь на прошлой версии', self._path)
            return self._data
        self._data = data
        self._mtime = mtime
        self._error_logged = False
        return data

    def _log_once(self, message: str, *args) -> None:
        if not self._error_logged:
            logger.error(message, *args)
            self._error_logged = True


_content = _ContentFile(CONTENT_PATH)


def _value(section: str, key: str, values: dict) -> str:
    raw = _content.get().get(section, {}).get(key)
    if raw is None:
        logger.error('CONTENT.md: нет ключа %s.%s', section, key)
        return ''
    return safe_format(raw, **values) if values else raw


def _lines(raw: str) -> list[str]:
    lines = (line.strip() for line in raw.splitlines())
    return [line for line in lines if line and not line.startswith('#')]


class Content:
    """Доступ к текстам. Плейсхолдеры подставляются через safe_format."""

    @staticmethod
    def prompt(name: str, **values) -> str:
        return _value('prompts', name, values)

    @staticmethod
    def label(name: str, **values) -> str:
        return _value('labels', name, values)

    @staticmethod
    def text(name: str, **values) -> str:
        return _value('texts', name, values)

    @staticmethod
    def items(name: str) -> list[str]:
        raw = _content.get().get('lists', {}).get(name)
        if raw is None:
            logger.error('CONTENT.md: нет списка lists.%s', name)
            return []
        return _lines(raw)


def validate_content() -> None:
    """Проверить структуру файла. Вызывается на старте бота.

    Отсутствующий ключ — ошибка запуска. Лишний ключ или секция — почти
    всегда опечатка в заголовке, поэтому о них предупреждаем: сам по себе
    такой заголовок молча ничего бы не сломал, но нужный текст при этом
    остался бы недоступным.
    """
    if not CONTENT_PATH.exists():
        raise FileNotFoundError(f'Не найден {CONTENT_PATH}')
    data = _content.get()
    if not data:
        raise ValueError(f'{CONTENT_PATH.name} пуст или не разобран — смотри лог')

    missing = [
        f'{section}.{key}'
        for section, keys in REQUIRED.items()
        for key in keys
        if not data.get(section, {}).get(key, '') and section != 'lists'
    ]
    # Списки могут быть пустыми по смыслу (стоп-лист), важно лишь наличие ключа
    missing += [
        f'lists.{key}' for key in REQUIRED['lists'] if key not in data.get('lists', {})
    ]
    if missing:
        raise ValueError(f'{CONTENT_PATH.name}: не хватает ключей: {", ".join(missing)}')

    unknown = [f'## {s}' for s in data if s not in REQUIRED]
    unknown += [
        f'{section}.{key}'
        for section, keys in REQUIRED.items()
        for key in data.get(section, {})
        if key not in keys
    ]
    if unknown:
        logger.warning(
            '%s: неизвестные заголовки (опечатка?): %s', CONTENT_PATH.name, ', '.join(unknown)
        )
