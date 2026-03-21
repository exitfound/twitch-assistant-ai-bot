import os
from dotenv import load_dotenv

load_dotenv()

class Twitch:
    CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
    CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
    BOT_ID = os.getenv('TWITCH_BOT_ID')
    CHANNEL = os.getenv('TWITCH_CHANNEL')
    BOT_TOKEN = os.getenv('TWITCH_BOT_TOKEN')
    BOT_REFRESH = os.getenv('TWITCH_BOT_REFRESH')

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'prompt.txt')


def _load_prompt() -> str:
    with open(PROMPT_PATH, encoding='utf-8') as f:
        return f.read().strip()


class Gemini:
    API_KEY = os.getenv('GEMINI_API_KEY')
    MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    TEMPERATURE = float(os.getenv('GEMINI_TEMPERATURE', '1.5'))
    THINKING_BUDGET = int(os.getenv('GEMINI_THINKING_BUDGET', '0'))

    @staticmethod
    def get_system_instruction() -> str:
        return _load_prompt()

class Caps:
    PROBABILITY = float(os.getenv('CAPS_PROBABILITY', '0.3'))

class Cooldown:
    SECONDS = int(os.getenv('COOLDOWN_SECONDS', '10'))
    MESSAGE = os.getenv('COOLDOWN_MESSAGE', 'гуляй, отвечу повторно через {seconds} сек.')

class Context:
    CHAT_MESSAGES = int(os.getenv('CONTEXT_CHAT_MESSAGES', '50'))
    SEARCH_RESULTS = int(os.getenv('CONTEXT_SEARCH_RESULTS', '10'))
    KNOWLEDGE_RANDOM = int(os.getenv('CONTEXT_KNOWLEDGE_RANDOM', '10'))

class Proactive:
    INTERVAL_MINUTES = int(os.getenv('PROACTIVE_INTERVAL_MINUTES', '15'))
    ENABLED = os.getenv('PROACTIVE_ENABLED', 'true').lower() in ('true', '1', 'yes')
