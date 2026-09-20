"""Sync of the emote list in CONTENT.md with the Twitch API.

Sources are declared in SOURCES: each returns emotes sorted into groups, and a
group becomes a comment line in the list. Adding a new source (7TV, BTTV, FFZ)
is one async function and one line in SOURCES; at the time of writing the
channel has no emotes on those services, so there is no code for them here.

The merge is non-destructive: existing lines are left alone, new ones are
appended to the end of their group. A full rewrite happens only on an explicit flag.
"""
import logging
import re

import httpx

from src.core.content import CONTENT_PATH

logger = logging.getLogger(__name__)

TOKEN_URL = 'https://id.twitch.tv/oauth2/token'
HELIX = 'https://api.twitch.tv/helix'
TIMEOUT = 20

# Group headers inside the emote list. They match the ones already in
# CONTENT.md – otherwise a repeated sync would create a second identical group.
GROUP_GLOBAL = 'Глобальные эмоты Twitch'
GROUP_FOLLOWER = 'Эмоты канала – фолловерские'
GROUP_BITS = 'Эмоты канала – за биты'
GROUP_OTHER = 'Эмоты канала – прочие'
TIER_NAMES = {'1000': 1, '2000': 2, '3000': 3}


class SyncError(Exception):
    """A sync error the user can understand."""


async def _app_token(client: httpx.AsyncClient, client_id: str, client_secret: str) -> str:
    """App access token: reading emotes needs no user scopes."""
    response = await client.post(TOKEN_URL, data={
        'client_id': client_id,
        'client_secret': client_secret,
        'grant_type': 'client_credentials',
    })
    if response.status_code != 200:
        raise SyncError(f'Twitch не выдал токен (HTTP {response.status_code}). Проверь TWITCH_CLIENT_ID/SECRET')
    return response.json()['access_token']


async def _broadcaster_id(client: httpx.AsyncClient, headers: dict, channel: str) -> str:
    response = await client.get(f'{HELIX}/users', params={'login': channel}, headers=headers)
    if response.status_code != 200:
        raise SyncError(f'Не удалось найти канал {channel} (HTTP {response.status_code})')
    data = response.json().get('data') or []
    if not data:
        raise SyncError(f'Канал {channel} не найден')
    return data[0]['id']


def _channel_group(emote: dict) -> str:
    kind = emote.get('emote_type')
    if kind == 'follower':
        return GROUP_FOLLOWER
    if kind == 'bitstier':
        return GROUP_BITS
    if kind == 'subscriptions':
        tier = TIER_NAMES.get(str(emote.get('tier') or ''))
        return f'Эмоты канала – сабские tier {tier}' if tier else GROUP_OTHER
    return GROUP_OTHER


async def fetch_channel(client: httpx.AsyncClient, headers: dict, channel: str) -> dict[str, list[str]]:
    """Channel emotes: follower ones, subscriber ones by tier, bits ones."""
    broadcaster_id = await _broadcaster_id(client, headers, channel)
    response = await client.get(f'{HELIX}/chat/emotes',
                                params={'broadcaster_id': broadcaster_id}, headers=headers)
    if response.status_code != 200:
        raise SyncError(f'Не удалось получить эмоты канала (HTTP {response.status_code})')
    groups: dict[str, list[str]] = {}
    for emote in response.json().get('data', []):
        groups.setdefault(_channel_group(emote), []).append(emote['name'])
    return groups


async def fetch_global(client: httpx.AsyncClient, headers: dict, channel: str) -> dict[str, list[str]]:
    """Global Twitch emotes – available to everyone and always rendered."""
    response = await client.get(f'{HELIX}/chat/emotes/global', headers=headers)
    if response.status_code != 200:
        raise SyncError(f'Не удалось получить глобальные эмоты (HTTP {response.status_code})')
    names = [emote['name'] for emote in response.json().get('data', [])]
    return {GROUP_GLOBAL: names} if names else {}


SOURCES = {
    'channel': fetch_channel,
    'global': fetch_global,
}


async def fetch(sources: list[str], client_id: str, client_secret: str,
                channel: str) -> dict[str, list[str]]:
    """Collect emotes from the listed sources into {group: [codes]}."""
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        raise SyncError(f'Неизвестный источник: {", ".join(unknown)}. Доступны: {", ".join(SOURCES)}')
    if not (client_id and client_secret and channel):
        raise SyncError('Нужны TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET и TWITCH_CHANNEL в .env')

    groups: dict[str, list[str]] = {}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        token = await _app_token(client, client_id, client_secret)
        headers = {'Client-Id': client_id, 'Authorization': f'Bearer {token}'}
        for name in sources:
            for group, names in (await SOURCES[name](client, headers, channel)).items():
                groups.setdefault(group, []).extend(names)
    return groups


# --- Editing the list in CONTENT.md -------------------------------------

_KEY_RE = re.compile(r'^###\s+(\S+)\s*$')
_HEADING_RE = re.compile(r'^#{2,3}\s+\S')


def _locate_emotes_block(lines: list[str]) -> tuple[int, int]:
    """Bounds of the value of the `### emotes` key inside `## lists`."""
    start = None
    for i, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if match and match.group(1) == 'emotes':
            start = i + 1
            break
    if start is None:
        raise SyncError(f'В {CONTENT_PATH.name} нет ключа "### emotes"')
    end = len(lines)
    for i in range(start, len(lines)):
        if _HEADING_RE.match(lines[i]):
            end = i
            break
    return start, end


def _group_end(block: list[str], header: str) -> int | None:
    """Index past the last entry of group `# header`, or None if there is no such group."""
    try:
        start = next(i for i, line in enumerate(block)
                     if line.strip().startswith('#') and line.strip().lstrip('#').strip() == header)
    except StopIteration:
        return None
    end = start + 1
    for i in range(start + 1, len(block)):
        if block[i].strip().startswith('#'):
            break
        if block[i].strip():
            end = i + 1
    return end


def merge(groups: dict[str, list[str]], replace: bool = False,
          write: bool = True) -> tuple[list[str], list[str]]:
    """Merge emotes into CONTENT.md. Returns (added, already present).

    write=False computes the result without touching the file (for --dry-run).
    """
    text = CONTENT_PATH.read_text(encoding='utf-8')
    lines = text.splitlines()
    start, end = _locate_emotes_block(lines)
    block = lines[start:end]

    if replace:
        # Keep the note on the key, rebuild everything else from scratch
        block = [line for line in block if line.strip().startswith('<!--')]

    existing = {line.strip() for line in block
                if line.strip() and not line.strip().startswith(('#', '<!--'))}

    added, skipped = [], []
    for header, names in groups.items():
        fresh = []
        for name in names:
            if name in existing or name in fresh:
                skipped.append(name)
            else:
                fresh.append(name)
                existing.add(name)
        if not fresh:
            continue
        added.extend(fresh)
        position = _group_end(block, header)
        if position is None:
            if block and block[-1].strip():
                block.append('')
            block.extend([f'# {header}', *fresh])
        else:
            block[position:position] = fresh

    if write and (added or replace):
        CONTENT_PATH.write_text(
            '\n'.join(lines[:start] + block + lines[end:]) + '\n', encoding='utf-8'
        )
    return added, skipped
