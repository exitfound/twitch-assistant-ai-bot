import asyncio
import logging

from google import genai
from google.genai import types

from src.config import Gemini

logger = logging.getLogger(__name__)

SAFETY_OFF = [
    types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='OFF'),
]

_client: genai.Client | None = None
_semaphore = asyncio.Semaphore(5)
GEMINI_TIMEOUT = 60


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=Gemini.API_KEY)
    return _client


def make_gen_config() -> types.GenerateContentConfig:
    config = types.GenerateContentConfig(
        system_instruction=Gemini.get_system_instruction(),
        temperature=Gemini.TEMPERATURE,
        safety_settings=SAFETY_OFF,
    )
    if Gemini.THINKING_BUDGET >= 0:
        config.thinking_config = types.ThinkingConfig(
            thinking_budget=Gemini.THINKING_BUDGET,
        )
    return config


async def generate(contents: str, config: types.GenerateContentConfig) -> str | None:
    async with _semaphore:
        try:
            response = await asyncio.wait_for(
                get_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents=contents,
                    config=config,
                ),
                timeout=GEMINI_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning('Gemini request timed out after %ds', GEMINI_TIMEOUT)
            return None
        try:
            return response.text
        except (ValueError, AttributeError):
            logger.debug('Empty/blocked Gemini response')
            return None
