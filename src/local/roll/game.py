"""Игра «залупа стрима»: единственное место, которое меняет таблицу rolls.

Бросок из чата и награды за баллы канала приходят из разных мест, а
состояние у них одно. Всё, что читает ролл и потом пишет новый, идёт под
общей блокировкой: иначе переброс, пришедший между чтением и записью
собственного !roll жертвы, затёр бы один из результатов.

Модуль ничего не пишет в чат и не знает про Twitch. Он возвращает Outcome,
а текст и судьбу баллов по нему решает вызывающий – хендлер команды или
обработчик награды.
"""
import asyncio
import dataclasses
import math
import random
import re
import time

from src.core.config import Rewards, Roll
from src.core.database import get_previous_stream_session, get_session_start, has_chatted
from src.local.roll.storage import (
    RollRow, activate_perks, add_perk, consume_perk, get_action_status, get_expired_curses,
    get_last_roll_session_before, get_perk, get_roll, get_session_champion, get_session_loser,
    has_action, save_action, save_roll, seconds_since_action, set_curse,
)

# Действия, которые покупаются за баллы канала
ACTION_EXTRA = 'extra'      # бросок за себя сверх бесплатных
ACTION_REROLL = 'reroll'    # бросок за другого, щит от него защищает
ACTION_CURSE = 'curse'      # бросок за другого с потолком, дальше потолок падает; щит пробивает
ACTION_SHIELD = 'shield'    # защита от переброса до конца сессии
ACTIONS = (ACTION_EXTRA, ACTION_REROLL, ACTION_CURSE, ACTION_SHIELD)

# Бонусы по итогам прошлого эфира: не покупаются, выдаются в начале нового
PERK_SHIELD = 'shield'      # китежанину – щит от перебросов
PERK_CURSE = 'curse'        # залупе – проклятие с жёстким сроком

# Итоги. OK – изменение применено, всё остальное – отказ с причиной
OK = 'ok'
NO_FREE_LEFT = 'no_free_left'           # !roll: бесплатные кончились
FREE_LEFT = 'free_left'                 # доп. ролл купили, хотя бесплатные ещё есть
BAD_TARGET = 'bad_target'               # в поле награды не ник
SELF_TARGET = 'self_target'             # перебросить или проклясть себя
NOT_ROLLED = 'not_rolled'               # цель сегодня не катала
SHIELDED = 'shielded'                   # у цели щит
ALREADY_SHIELDED = 'already_shielded'   # щит купили повторно
ALREADY_CURSED = 'already_cursed'       # цель уже прокляли
PROTECTED = 'protected'                 # цель недавно перебросили, защита ещё действует
UNKNOWN_TARGET = 'unknown_target'       # переброс за ник, которого нет ни в игре, ни в чате
PERK_SHIELDED = 'perk_shielded'         # у цели щит китежанина прошлого эфира
DUPLICATE = 'duplicate'                 # Twitch прислал ту же награду ещё раз

# Логин Twitch: латиница, цифры и подчёркивание, до 25 символов
_NICK_RE = re.compile(r'[a-z0-9_]{1,25}')

_lock = asyncio.Lock()


@dataclasses.dataclass(frozen=True)
class Outcome:
    status: str
    target: str | None = None               # чей ролл затронут
    old_value: int | None = None            # ролл до операции
    value: int | None = None                # ролл после операции
    free_left: int | None = None            # бесплатных бросков осталось, None – лимита нет
    loser: tuple[str, int] | None = None    # залупа сессии после операции
    champion: tuple[str, int] | None = None # китежанин сессии, None – если он же залупа
    # Проклятие цели, если оно действовало на этот бросок
    ceiling: int | None = None              # потолок этого броска
    next_ceiling: int | None = None         # потолок следующего
    curse_minutes_left: int | None = None   # потолок на дне: через сколько минут спадёт
    protect_minutes_left: int | None = None # защита после переброса: сколько минут осталось

    @property
    def ok(self) -> bool:
        return self.status == OK


def _throw(ceiling: int | None = None) -> int:
    top = Roll.MAX if ceiling is None else max(Roll.MIN, min(ceiling, Roll.MAX))
    return random.randint(Roll.MIN, top)


def parse_nick(raw: str) -> str | None:
    """Ник из поля награды: первое слово без @ и хвостовой пунктуации.

    Зрители пишут «@Nick», «nick,» или «ник и ещё что-то». Всё, что после
    первого слова, отбрасывается; не похожее на логин – None.
    """
    words = raw.strip().split()
    if not words:
        return None
    nick = words[0].lstrip('@').rstrip(',.:;!?').lower()
    return nick if _NICK_RE.fullmatch(nick) else None


# --- проклятие ---------------------------------------------------------------

def _curse_of(row: RollRow | None) -> tuple[int, float | None] | None:
    """Действующее проклятие: (потолок ближайшего броска, когда он встал на дно).

    Снимать проклятие отдельно не нужно: как только потолок пробыл на дне
    дольше CURSE_HOLD_MINUTES, строка просто перестаёт считаться проклятой.
    Новая сессия – новая строка, проклятия в ней нет. У проклятия залупы
    прошлого эфира есть ещё жёсткий срок curse_until.
    """
    if row is None or row.curse_ceiling is None:
        return None
    if row.curse_until is not None and time.time() >= row.curse_until:
        return None
    floor_at = row.curse_floor_at
    if floor_at is not None and time.time() - floor_at >= Rewards.CURSE_HOLD_MINUTES * 60:
        return None
    return row.curse_ceiling, floor_at


def _lowered(ceiling: int, floor_at: float | None) -> tuple[int, float | None]:
    """Потолок после броска проклятого: на шаг ниже, но не ниже дна.

    Отсчёт до снятия запускается в тот момент, когда потолок впервые встал
    на дно, и дальше не сдвигается.
    """
    next_ceiling = max(Rewards.CURSE_FLOOR, ceiling - Rewards.CURSE_STEP)
    if floor_at is None and next_ceiling == Rewards.CURSE_FLOOR:
        floor_at = time.time()
    return next_ceiling, floor_at


def _minutes_left(floor_at: float | None) -> int | None:
    if floor_at is None:
        return None
    seconds = floor_at + Rewards.CURSE_HOLD_MINUTES * 60 - time.time()
    return max(1, math.ceil(seconds / 60))


async def _throw_for(
    session_id: str, user: str, row: RollRow | None, *, free_throw: bool, limit: int | None = None,
) -> dict:
    """Бросок по строке игрока – свой или чужой переброс.

    Проклятому бросок идёт с потолком, и потолок опускается на шаг: любой
    бросок по жертве, от кого бы он ни был, приближает проклятие к дну.
    """
    curse = _curse_of(row)
    until = row.curse_until if curse is not None else None
    if curse is None:
        # Проклятие залупы прошлого эфира ложится на первый бросок по ней
        until = await _take_perk_curse(session_id, user)
        if until is not None:
            curse = (Rewards.CURSE_CEILING, None)
    if curse is None:
        value = _throw()
        await save_roll(session_id, user, value, free_throw=free_throw, limit=limit)
        return {'value': value}
    ceiling, floor_at = curse
    value = _throw(ceiling)
    await save_roll(session_id, user, value, free_throw=free_throw, limit=limit)
    next_ceiling, floor_at = _lowered(ceiling, floor_at)
    await set_curse(session_id, user, next_ceiling, floor_at, until)
    return {
        'value': value, 'ceiling': ceiling, 'next_ceiling': next_ceiling,
        'curse_minutes_left': _minutes_left(floor_at),
    }


async def _standings(session_id: str) -> dict:
    """Залупа и китежанин сессии после броска.

    Китежанина не показываем, если это тот же человек, что и залупа: так
    бывает, когда катал один игрок или все выбросили одно число, – хвалить
    не на фоне кого.
    """
    loser = await get_session_loser(session_id)
    champion = await get_session_champion(session_id)
    if champion is not None and loser is not None and champion[0] == loser[0]:
        champion = None
    return {'loser': loser, 'champion': champion}


# --- бонусы по итогам прошлого эфира ---------------------------------------------

async def _activate_perks(session_id: str, user: str) -> list[str]:
    """Первое появление игрока в эфире запускает отсчёт его бонусов."""
    now = time.time()
    return await activate_perks(session_id, user, now, now + Roll.PERK_MINUTES * 60)


async def _perk_shield_left(session_id: str, user: str) -> int | None:
    """Минут до конца щита китежанина. None – щита нет.

    Переброс по игроку – тоже его появление: щит включается и сразу защищает.
    """
    await _activate_perks(session_id, user)
    perk = await get_perk(session_id, user, PERK_SHIELD)
    if perk is None or perk.active_until is None:
        return None
    seconds = perk.active_until - time.time()
    return max(1, math.ceil(seconds / 60)) if seconds > 0 else None


async def _take_perk_curse(session_id: str, user: str) -> float | None:
    """Забрать проклятие залупы для первого броска по ней. Возвращает его срок.

    Проклятие одно: легло на бросок – второй раз не выдаётся, даже если его
    сняли раньше срока. Не появился в эфире за отведённое время – сгорело.
    """
    await _activate_perks(session_id, user)
    perk = await get_perk(session_id, user, PERK_CURSE)
    if perk is None or perk.consumed or perk.active_until is None or perk.active_until <= time.time():
        return None
    await consume_perk(session_id, user, PERK_CURSE)
    return perk.active_until


async def _previous_session(session_id: str) -> str | None:
    """Сессия, по итогам которой выдаются бонусы: эфир, шедший перед этим.

    Прошлый эфир прошёл без роллов – бонусов нет, более ранние эфиры не в счёт.
    Эфиров раньше не записано (первый эфир после перехода с сессий-дат) – берётся
    последняя старая сессия, броски в которой закончились до начала этого эфира.
    """
    previous = await get_previous_stream_session(session_id)
    if previous is not None:
        return previous
    start = await get_session_start(session_id)
    return await get_last_roll_session_before(session_id, start if start is not None else time.time())


async def grant_perks(session_id: str) -> tuple[str | None, str | None] | None:
    """Выдать бонусы по итогам прошлого эфира: (китежанин, залупа).

    None – выдавать нечего или всё уже выдано. Вызов безопасно повторять после
    перезапуска и обрыва эфира: второй раз ничего не выдаётся.
    """
    async with _lock:
        previous = await _previous_session(session_id)
        if previous is None:
            return None
        loser = await get_session_loser(previous)
        champion = await get_session_champion(previous)
        if champion is not None and loser is not None and champion[0] == loser[0]:
            champion = None
        shielded = champion is not None and await add_perk(session_id, champion[0], PERK_SHIELD, previous)
        cursed = loser is not None and await add_perk(session_id, loser[0], PERK_CURSE, previous)
    if not (shielded or cursed):
        return None
    return (champion[0] if shielded else None, loser[0] if cursed else None)


async def appear(session_id: str, user: str) -> list[str]:
    """Игрок появился в чате эфира: запустить отсчёт его бонусов. Какие запущены."""
    async with _lock:
        return await _activate_perks(session_id, user)


# --- операции ----------------------------------------------------------------

async def free_throw(
    session_id: str, user: str, *, limit: int, unlimited: bool = False,
) -> Outcome:
    """Бесплатный !roll: не больше limit за сессию.

    limit зависит от статуса зрителя (см. free_limit_for() в команде) и
    запоминается в строке игрока: выкуп награды приходит без значков.
    unlimited – лимит не действует (стример). Бросок всё равно идёт в счётчик,
    чтобы доп. ролл за баллы у него работал как у всех. free_left у такого
    итога None: писать про остаток нечего.
    """
    async with _lock:
        row = await get_roll(session_id, user)
        used = row.free_throws if row else 0
        if not unlimited and used >= limit:
            return Outcome(NO_FREE_LEFT, target=user, free_left=0)
        throw = await _throw_for(session_id, user, row, free_throw=True, limit=limit)
        standings = await _standings(session_id)
    return Outcome(
        OK, target=user, old_value=row.value if row else None,
        free_left=None if unlimited else max(0, limit - used - 1),
        **standings, **throw,
    )


async def lift_expired_curses(session_id: str) -> list[str]:
    """Снять кончившиеся проклятия: потолок пробыл на дне дольше CURSE_HOLD_MINUTES
    или вышел жёсткий срок проклятия залупы прошлого эфира.

    Механике снятие не нужно: _curse_of() и так не считает такую строку
    проклятой. Оно нужно, чтобы бот сказал об этом в чат ровно один раз –
    очищенная строка в следующую выборку не попадёт. Под общей блокировкой,
    чтобы не стереть проклятие, наложенное заново между выборкой и записью.
    """
    async with _lock:
        now = time.time()
        users = await get_expired_curses(session_id, now - Rewards.CURSE_HOLD_MINUTES * 60, now)
        for user in users:
            await set_curse(session_id, user, None, None)
    return users


async def redeem(
    action: str, session_id: str, actor: str, user_input: str, redemption_id: str,
) -> Outcome:
    """Применить купленную награду и записать итог в журнал roll_actions.

    Журнал заодно защищает от повторов: Twitch может прислать одно событие
    дважды, и второй раз бросать нельзя. Проверка и запись идут под той же
    блокировкой, что и сам бросок, поэтому повтор не проскочит между ними.
    """
    async with _lock:
        if await get_action_status(redemption_id) is not None:
            return Outcome(DUPLICATE)
        if action == ACTION_EXTRA:
            outcome = await _extra(session_id, actor)
        elif action == ACTION_SHIELD:
            outcome = await _shield(session_id, actor)
        elif action == ACTION_REROLL:
            outcome = await _reroll(session_id, actor, user_input)
        elif action == ACTION_CURSE:
            outcome = await _curse(session_id, actor, user_input)
        else:
            raise ValueError(f'Неизвестное действие: {action}')
        await save_action(
            redemption_id, session_id, action, actor, user_input,
            outcome.target, outcome.old_value, outcome.value, outcome.status,
        )
    return outcome


async def _extra(session_id: str, actor: str) -> Outcome:
    row = await get_roll(session_id, actor)
    used = row.free_throws if row else 0
    # В событии выкупа значков нет, поэтому лимит берём из строки игрока –
    # его записал его же бросок из чата. Не катал ни разу – базовый лимит
    limit = (row.free_limit if row and row.free_limit else None) or Roll.FREE_PER_SESSION
    if used < limit:
        # Баллы за бросок, который и так бесплатный, – почти наверняка промах
        return Outcome(FREE_LEFT, target=actor, free_left=limit - used)
    throw = await _throw_for(session_id, actor, row, free_throw=False)
    return Outcome(
        OK, target=actor, old_value=row.value if row else None,
        **await _standings(session_id), **throw,
    )


async def _shield(session_id: str, actor: str) -> Outcome:
    # Сам щит – это успешная запись в журнале, отдельного состояния у него нет
    if await has_action(session_id, ACTION_SHIELD, actor, OK):
        return Outcome(ALREADY_SHIELDED, target=actor)
    return Outcome(OK, target=actor)


async def _target(
    session_id: str, actor: str, user_input: str, *, require_roll: bool,
) -> tuple[str, RollRow | None] | Outcome:
    """Цель переброса или проклятия – или отказ, если трогать некого.

    require_roll – цель должна была катать в этой сессии: проклятие вешается
    только на того, кто уже в игре. Переброс бросает и за того, кто ещё не
    катал, но только за ник, который хоть раз писал в чате: иначе опечатка в
    поле награды завела бы ролл несуществующему игроку, и он мог бы стать
    залупой стрима.
    """
    target = parse_nick(user_input)
    if target is None:
        return Outcome(BAD_TARGET)
    if target == actor:
        return Outcome(SELF_TARGET, target=target)
    row = await get_roll(session_id, target)
    if row is None:
        if require_roll:
            return Outcome(NOT_ROLLED, target=target)
        if not await has_chatted(target):
            return Outcome(UNKNOWN_TARGET, target=target)
    return target, row


async def _protection_left(session_id: str, target: str) -> int | None:
    """Минут до конца защиты после чужого переброса. None – защиты нет.

    Лимит Twitch на зрителя считает каждого атакующего отдельно, поэтому
    несколько человек могли бы перебрасывать одну цель подряд. Окно идёт от
    последнего успешного переброса, а отклонённые попытки в журнал пишутся
    не как ok и его не продлевают.
    """
    window = Rewards.REROLL_PROTECT_MINUTES * 60
    if not window:
        return None
    elapsed = await seconds_since_action(session_id, ACTION_REROLL, target, OK)
    if elapsed is None or elapsed >= window:
        return None
    return max(1, math.ceil((window - elapsed) / 60))


async def _reroll(session_id: str, actor: str, user_input: str) -> Outcome:
    found = await _target(session_id, actor, user_input, require_roll=False)
    if isinstance(found, Outcome):
        return found
    target, row = found
    if await has_action(session_id, ACTION_SHIELD, target, OK):
        return Outcome(SHIELDED, target=target)
    perk_left = await _perk_shield_left(session_id, target)
    if perk_left is not None:
        return Outcome(PERK_SHIELDED, target=target, protect_minutes_left=perk_left)
    protect_left = await _protection_left(session_id, target)
    if protect_left is not None:
        return Outcome(PROTECTED, target=target, protect_minutes_left=protect_left)
    # На проклятого переброс идёт с его потолком и опускает потолок так же,
    # как собственный бросок жертвы. Бесплатные броски жертвы не тратятся
    throw = await _throw_for(session_id, target, row, free_throw=False)
    return Outcome(
        OK, target=target, old_value=row.value if row else None,
        **await _standings(session_id), **throw,
    )


async def _curse(session_id: str, actor: str, user_input: str) -> Outcome:
    found = await _target(session_id, actor, user_input, require_roll=True)
    if isinstance(found, Outcome):
        return found
    target, row = found
    # Щит не проверяется намеренно: проклятие его пробивает, в этом его
    # отличие от дешёвого переброса
    if _curse_of(row) is not None:
        # Новое проклятие вернуло бы потолок наверх – жертве это подарок
        return Outcome(ALREADY_CURSED, target=target)
    ceiling = Rewards.CURSE_CEILING
    value = _throw(ceiling)
    await save_roll(session_id, target, value, free_throw=False)
    next_ceiling, floor_at = _lowered(ceiling, None)
    await set_curse(session_id, target, next_ceiling, floor_at)
    return Outcome(
        OK, target=target, old_value=row.value, value=value,
        ceiling=ceiling, next_ceiling=next_ceiling, curse_minutes_left=_minutes_left(floor_at),
        **await _standings(session_id),
    )
