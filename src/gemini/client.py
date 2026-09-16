import asyncio
import logging
import random

import httpx
from google import genai
from google.genai import errors, types

from src.core.config import Gemini
from src.core.content import Content

logger = logging.getLogger(__name__)

SAFETY_OFF = [
    types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='OFF'),
]

_client: genai.Client | None = None
_semaphore = asyncio.Semaphore(Gemini.CONCURRENCY)

RETRY_BASE_DELAY = 1.0


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=Gemini.API_KEY)
    return _client


def make_gen_config() -> types.GenerateContentConfig:
    config = types.GenerateContentConfig(
        system_instruction=Content.prompt('system'),
        temperature=Gemini.TEMPERATURE,
        safety_settings=SAFETY_OFF,
    )
    if Gemini.THINKING_BUDGET >= 0:
        config.thinking_config = types.ThinkingConfig(
            thinking_budget=Gemini.THINKING_BUDGET,
        )
    return config


def _is_transient(error: Exception) -> bool:
    """Стоит ли повторять запрос: перегрузка сервера, лимит или сеть.

    Таймаут сюда не входит намеренно: зритель в чате уже ждёт Gemini.TIMEOUT
    секунд, и повторные попытки растянули бы ответ на минуты.
    """
    if isinstance(error, errors.ServerError):
        return True
    if isinstance(error, errors.APIError):
        return error.code in (408, 429)
    return isinstance(error, httpx.TransportError)


async def generate(contents: str, config: types.GenerateContentConfig) -> str | None:
    """Запрос к Gemini. Возвращает None при таймауте, блокировке или отказе API."""
    async with _semaphore:
        for attempt in range(Gemini.RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    get_client().aio.models.generate_content(
                        model=Gemini.MODEL,
                        contents=contents,
                        config=config,
                    ),
                    timeout=Gemini.TIMEOUT,
                )
            except Exception as e:
                last = attempt == Gemini.RETRIES
                if not _is_transient(e) or last:
                    if isinstance(e, asyncio.TimeoutError):
                        logger.warning('Gemini: таймаут %d с (попыток: %d)', Gemini.TIMEOUT, attempt + 1)
                    else:
                        logger.warning('Gemini: запрос не удался (попыток: %d): %s', attempt + 1, e)
                    return None
                delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                logger.info('Gemini: %s, повтор через %.1f с', type(e).__name__, delay)
                await asyncio.sleep(delay)
                continue

            try:
                return response.text
            except (ValueError, AttributeError):
                logger.debug('Gemini: пустой или заблокированный ответ')
                return None
    return None
