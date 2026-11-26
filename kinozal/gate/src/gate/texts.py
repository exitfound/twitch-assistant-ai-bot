"""The entry page's texts in Russian and English, and the language a browser asks for."""

LANGS = ("ru", "en")
RU_READERS = ("ru", "uk", "be", "kk", "ky", "uz", "tg", "tk", "az", "hy")
TEXT = {
    "ru": {
        "title": "Кинозал",
        "switch_label": "Switch to English",
        "closed_lead": "Сейчас сеанса нет",
        "closed_hint": "Когда начнётся кино, ссылка и код появятся в чате Twitch.",
        "now": "Сегодняшний фильм:",
        "enter": "Войти",
        "missed": "Пропустил код – напиши !film в чате",
        "code_after_twitch": "Теперь код из чата Twitch",
        "followers_only": "Вход только для фолловеров и сабов канала {chan}",
        "twitch": "Войти через Twitch",
        "then_code": "После введи код из чата Twitch для просмотра фильма",
        "code": "Введи код из чата Twitch",
        "need_twitch": "Сначала войди через Twitch.",
        "too_many": "Слишком много попыток. Подожди пару минут.",
        "bad_code": "Неверный код.",
        "twitch_cancelled": "Вход через Twitch отменён. Без него в кинозал не войти – попробуй ещё раз.",
        "twitch_failed": "Вход через Twitch не удался, попробуй ещё раз.",
        "banned": "Вход в кинозал для тебя закрыт.",
        "not_follower": "Вход через Twitch – для фолловеров и сабов канала {chan}. Зафоловь канал и попробуй снова.",
        "unavailable": "Twitch или чат кинозала не отвечает. Попробуй ещё раз через минуту.",
        "to_hall": "В кинозал",
        "to_entry": "Ко входу",
    },
    "en": {
        "title": "Cinema",
        "switch_label": "Переключить на русский",
        "closed_lead": "There is no session right now",
        "closed_hint": "When the film starts, the link and the code appear in the Twitch chat.",
        "now": "Tonight's film:",
        "enter": "Enter",
        "missed": "Missed the code? Type !film in the chat",
        "code_after_twitch": "Now the code from the Twitch chat",
        "followers_only": "Only for followers and subscribers of {chan}",
        "twitch": "Sign in with Twitch",
        "then_code": "Then enter the code from the Twitch chat to watch the film",
        "code": "Enter the code from the Twitch chat",
        "need_twitch": "Sign in with Twitch first.",
        "too_many": "Too many attempts. Wait a couple of minutes.",
        "bad_code": "Wrong code.",
        "twitch_cancelled": "Twitch sign-in was cancelled. The cinema opens only after it – try again.",
        "twitch_failed": "Twitch sign-in failed, try again.",
        "banned": "The cinema is closed to you.",
        "not_follower": "Twitch sign-in is for followers and subscribers of {chan}. Follow the channel and try again.",
        "unavailable": "Twitch or the cinema chat is not answering. Try again in a minute.",
        "to_hall": "To the cinema",
        "to_entry": "To the entry page",
    },
}


def browser_lang(header: str) -> str:
    """ru when the browser's preferred language reads Russian, else en. Without a header, ru."""
    best, best_q = "", -1.0
    for part in header.split(","):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if tag and q > best_q:
            best, best_q = tag.split("-")[0].strip().lower(), q
    if not best or best == "*":
        return LANGS[0]
    return "ru" if best in RU_READERS else "en"


def tr(lang: str, key: str, **values: str) -> str:
    text = TEXT[lang][key]
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text
