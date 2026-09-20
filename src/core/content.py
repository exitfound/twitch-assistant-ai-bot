"""Bot texts: a single CONTENT.md, hot-reloaded by mtime.

Everything the bot says – prompts, context headings, chat replies, the emote and
follow lists – lives in this one file and is edited without a restart; the environment
belongs to src/core/config.py. The format is Markdown, so Russian prose needs no
quoting or escaping:

    ## section
    ### key
    value up to the next heading

A section's text before its first `###` is a note and is ignored, as are
`<!-- ... -->` comments, multi-line ones included. Markdown is hard to break
syntactically, so validation checks meaning instead: duplicate keys and unclosed
sections are logged, missing or extra keys are caught by validate_content().
"""
import logging
import re
from pathlib import Path

# CONTENT_PATH may be moved by BOT_CONTENT_PATH – see src/core/paths.py. It keeps
# this module's name because src/cli/emotes.py imports it from here
from src.core.paths import CONTENT_PATH
from src.core.utils import safe_format

logger = logging.getLogger(__name__)

SECTION_RE = re.compile(r'^##\s+(\S+)\s*$')
KEY_RE = re.compile(r'^###\s+(\S+)\s*$')

# Keys the bot cannot work without – checked at startup.
REQUIRED = {
    'prompts': (
        'system', 'ask', 'ask_followup', 'summary', 'summary_request', 'summary_request_previous', 'summary_previous', 'summary_tail', 'who', 'versus',
        'picture',
        'proactive_user', 'proactive_general',
        'user_question', 'interaction_line',
        'memory_chronicle', 'memory_chronicle_merge', 'memory_profile', 'memory_hint',
    ),
    'labels': (
        'facts', 'chat', 'channel', 'language',
        'user_facts', 'user_messages', 'user_interactions',
        'user_events', 'user_sample', 'user_relations', 'user_recent', 'who_said',
        'prev_stream', 'people', 'chronicle', 'replied',
    ),
    'texts': (
        'help', 'help_announce', 'stats_self', 'stats_self_day', 'stats_stream', 'stats_day',
        'stats_total', 'stats_user', 'stats_user_day', 'stats_unknown',
        'cooldown_local', 'cooldown_gemini', 'role_denied', 'role_denied_sub',
        'follow_required', 'quota_exceeded', 'quota_channel',
        'roll_loser_self', 'roll_loser_other', 'roll_free_left', 'roll_champion',
        'roll_cursed_self', 'roll_cursed_other',
        'roll_curse_step', 'roll_curse_hold', 'roll_curse_lifted',
        'roll_no_free', 'roll_no_free_reward', 'roll_error', 'roll_offline',
        'rollstat_self', 'rollstat_none', 'rollstat_cursed', 'rollstat_curse_hold',
        'rollstat_shield', 'rollstat_perk_shield', 'rollstat_loser', 'rollstat_nobody',
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
        'ask_usage', 'ask_error',
        'ascii_usage', 'ascii_bad_url', 'ascii_failed', 'ascii_too_big',
        'ascii_blocked', 'ascii_unchecked', 'ascii_no_left',
        'summary_empty', 'summary_error', 'summary_no_left', 'summary_no_previous',
        'who_usage', 'who_unknown', 'who_failed', 'who_no_left',
        'versus_usage', 'versus_unknown', 'versus_unknown_one', 'versus_failed', 'versus_no_left',
        'no_answer', 'filtered', 'gen_error', 'gen_failed',
    ),
    'lists': ('emotes', 'follow', 'banned'),
}


def parse(raw: str) -> dict[str, dict[str, str]]:
    """Parse CONTENT.md into {section: {key: value}}.

    Duplicate keys are logged; the last definition wins – edits are usually
    appended further down.
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
        # Notes <!-- ... -->, multi-line ones included, never reach the values
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
    """CONTENT.md, re-read when its mtime changes."""

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
            self._log_once('Не удалось прочитать %s: %s – остаюсь на прошлой версии', self._path, e)
            return self._data
        data = parse(raw)
        if not data:
            # An empty parse most likely means the headings were wiped; the previous version is better.
            self._mtime = mtime
            self._log_once('%s не содержит секций – остаюсь на прошлой версии', self._path)
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
    """Access to texts. Placeholders are substituted via safe_format."""

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
    """Check the file structure. Called at bot startup.

    A missing key is a startup error. An extra key or section only warns: it is
    almost always a typo in a heading, which breaks nothing by itself but leaves the
    intended text unreachable.
    """
    if not CONTENT_PATH.exists():
        raise FileNotFoundError(f'Не найден {CONTENT_PATH}')
    data = _content.get()
    if not data:
        raise ValueError(f'{CONTENT_PATH.name} пуст или не разобран – смотри лог')

    missing = [
        f'{section}.{key}'
        for section, keys in REQUIRED.items()
        for key in keys
        if not data.get(section, {}).get(key, '') and section != 'lists'
    ]
    # Lists may legitimately be empty (the stop-list); only the presence of the key matters
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
