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

# The picture check (!ascii) is the one request that wants Google's classifier on: it
# vets a picture from a random viewer instead of voicing the persona. Harassment and hate
# stay off there too – memes are made of them, and blocking would refuse ordinary pictures
SAFETY_CHECK = [
    types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='OFF'),
    types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_MEDIUM_AND_ABOVE'),
    types.SafetySetting(category='HARM_CATEGORY_CIVIC_INTEGRITY', threshold='OFF'),
]

_client: genai.Client | None = None
_semaphore = asyncio.Semaphore(Gemini.CONCURRENCY)

RETRY_BASE_DELAY = 1.0

# Finish reasons of an answer cut off by Gemini's output filter
_OUTPUT_BLOCKS = {
    types.FinishReason.PROHIBITED_CONTENT, types.FinishReason.SAFETY,
    types.FinishReason.BLOCKLIST, types.FinishReason.SPII,
}

# Tokens spent since start: input, of it served from Gemini's cache, output.
# Read by CLI commands to report what a run cost
usage = {'prompt': 0, 'cached': 0, 'output': 0}

# Gemini 2.5 Flash, paid tier, $ per million tokens – only for the CLI's estimates
PRICE_INPUT = 0.30
PRICE_OUTPUT = 2.50


def cost_estimate(prompt_tokens: int, output_tokens: int = 0) -> float:
    """Dollars for that many tokens, at the full input price (the cache discount is ignored)."""
    return (prompt_tokens * PRICE_INPUT + output_tokens * PRICE_OUTPUT) / 1_000_000


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=Gemini.API_KEY)
    return _client


def make_gen_config(*, system: str | None = None, temperature: float | None = None,
                    persona: bool = True,
                    safety: list[types.SafetySetting] | None = None) -> types.GenerateContentConfig:
    """The request config: persona, chat temperature and safety filters off by default.

    A command with its own instruction, temperature or filters passes them in rather than
    building a config of its own: a hand-built config easily omits thinking_config,
    and GEMINI_THINKING_BUDGET is then replaced by dynamic thinking, billed separately.
    persona=False and no system – a request with no system instruction at all.
    """
    if system is None and persona:
        system = Content.prompt('system')
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=Gemini.TEMPERATURE if temperature is None else temperature,
        safety_settings=SAFETY_OFF if safety is None else safety,
    )
    if Gemini.THINKING_BUDGET >= 0:
        config.thinking_config = types.ThinkingConfig(
            thinking_budget=Gemini.THINKING_BUDGET,
        )
    return config


def _is_transient(error: Exception) -> bool:
    """Whether the request is worth retrying: server overload, rate limit or network.

    A timeout is deliberately excluded: the viewer in chat is already waiting
    Gemini.TIMEOUT seconds, and retries would stretch the answer to minutes.
    """
    if isinstance(error, errors.ServerError):
        return True
    if isinstance(error, errors.APIError):
        return error.code in (408, 429)
    return isinstance(error, httpx.TransportError)


async def generate(contents: str | list, config: types.GenerateContentConfig) -> str | None:
    """A Gemini request for chat, within Gemini.ANSWER_DEADLINE. Returns None on timeout,
    block or API refusal.

    contents is either text or a list of parts: that is how !ascii sends the
    picture itself along with the question.
    """
    try:
        async with asyncio.timeout(Gemini.ANSWER_DEADLINE):
            text, _ = await generate_checked(contents, config)
    except TimeoutError:
        logger.warning('Gemini: ответ не уложился в %d с', Gemini.ANSWER_DEADLINE)
        return None
    return text


# Why Gemini returned no text though the request went through
BLOCK_INPUT = 'input'
BLOCK_OUTPUT = 'output'
# Gemini answered, but with no text and no filter named (RECITATION, OTHER, …).
# Unlike a timeout this repeats for the same prompt, so it must not be retried forever
EMPTY = 'empty'
# The API refused the request itself (400 INVALID_ARGUMENT or 413: bad argument, schema,
# size). Also repeats for the same prompt, so it is a failure of the prompt, not an outage
ERROR = 'error'


def _prompt_error(e: Exception) -> bool:
    """The request was refused for what it contains. A bad key, a retired model or an
    unsupported region (403, 404, FAILED_PRECONDITION, API_KEY_INVALID) fail every prompt
    alike and count as an outage."""
    if not isinstance(e, errors.APIError) or e.code not in (400, 413):
        return False
    if e.status not in (None, 'INVALID_ARGUMENT'):
        return False
    return 'api key' not in f'{e.message} {e.details}'.lower().replace('_', ' ')


async def generate_checked(contents: str | list,
                           config: types.GenerateContentConfig) -> tuple[str | None, str | None]:
    """Like generate(), plus why there is no text: BLOCK_INPUT, BLOCK_OUTPUT, EMPTY
    (answered with nothing), ERROR (the API rejected the request for its content), or
    None – no answer at all (timeout, network, server errors past the retries, a bad key).

    Neither filter can be switched off. The input filter (BLOCK_INPUT) judges the
    whole prompt, so a caller with a big input can split it and try again. The
    output filter (BLOCK_OUTPUT) stops the answer halfway and is random: the same
    prompt usually passes on the next try. Both make sense to retry, a timeout
    or an unreachable Gemini does not.
    """
    for attempt in range(Gemini.RETRIES + 1):
        try:
            # A slot per attempt: a request waiting out its retry pause must not
            # hold one while other viewers queue behind it
            async with _semaphore, asyncio.timeout(Gemini.TIMEOUT):
                response = await get_client().aio.models.generate_content(
                    model=Gemini.MODEL,
                    contents=contents,
                    config=config,
                )
        except Exception as e:
            last = attempt == Gemini.RETRIES
            if not _is_transient(e) or last:
                if isinstance(e, TimeoutError):
                    logger.warning('Gemini: таймаут %d с (попыток: %d)', Gemini.TIMEOUT, attempt + 1)
                else:
                    logger.warning('Gemini: запрос не удался (попыток: %d): %s', attempt + 1, e)
                if _prompt_error(e):
                    return None, ERROR
                return None, None
            delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
            logger.info('Gemini: %s, повтор через %.1f с', type(e).__name__, delay)
            await asyncio.sleep(delay)
            continue

        meta = response.usage_metadata
        if meta is not None:
            usage['prompt'] += meta.prompt_token_count or 0
            usage['cached'] += meta.cached_content_token_count or 0
            usage['output'] += (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        feedback = response.prompt_feedback
        if feedback and feedback.block_reason:
            return None, BLOCK_INPUT
        try:
            text = response.text
        except (ValueError, AttributeError):
            text = None
        if text:
            # An answer the output filter cut short is still returned: a chat
            # answer sends whatever came back
            return text, None
        candidate = response.candidates[0] if response.candidates else None
        if candidate and candidate.finish_reason in _OUTPUT_BLOCKS:
            logger.debug('Gemini: ответ остановлен фильтром (%s)', candidate.finish_reason)
            return None, BLOCK_OUTPUT
        logger.debug('Gemini: пустой ответ (%s)', candidate.finish_reason if candidate else 'нет кандидатов')
        return None, EMPTY
    return None, None
