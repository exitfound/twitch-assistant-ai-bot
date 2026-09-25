"""The bot's SQLite storage, one import away: re-exports src/core/db/.

Callers import from here and do not care which module of the package holds a query.
Module state (the connection, the knowledge id cache) lives in its own module –
src/core/db/connection.py and src/core/db/knowledge.py – and is patched there.
"""
from src.core.db.chat import (
    get_chat_after, get_last_chat_session, get_previous_chat_session, get_recent_chat,
    get_session_stats, get_total_stats, get_user_messages, get_user_stats, has_chatted,
    save_chat_message,
)
from src.core.db.connection import BUSY_TIMEOUT, DB_PATH, backup_db, close_db, get_db, transaction, vacuum_db
from src.core.db.interactions import (
    get_last_tagged_interaction, get_tagged_answers, get_user_interactions, save_bot_interaction,
)
from src.core.db.knowledge import (
    KNOWLEDGE_IDS_TTL, get_all_facts, get_random_knowledge, get_relevant_facts,
    invalidate_knowledge_cache, search_context,
)
from src.core.db.quota import (
    OFFLINE_LIMIT_WINDOW_MINUTES, count_all_bot_uses, count_bot_uses, count_bot_uses_since,
    count_bot_uses_this_stream, count_channel_bot_uses, forget_bot_use, oldest_bot_use_age, record_bot_use,
)
from src.core.db.schema import init_db
from src.core.db.streams import (
    StreamRow, end_stream, get_last_stream, get_previous_stream_session, get_session_start,
    get_stream, last_chat_time, reopen_stream, save_stream,
)

__all__ = [
    'BUSY_TIMEOUT', 'DB_PATH', 'KNOWLEDGE_IDS_TTL', 'OFFLINE_LIMIT_WINDOW_MINUTES', 'StreamRow',
    'backup_db', 'close_db', 'count_all_bot_uses', 'count_bot_uses', 'count_bot_uses_since',
    'count_bot_uses_this_stream', 'count_channel_bot_uses', 'end_stream', 'forget_bot_use',
    'get_all_facts', 'get_chat_after',
    'get_db', 'get_last_chat_session', 'get_last_stream', 'get_last_tagged_interaction',
    'get_previous_chat_session', 'get_previous_stream_session', 'get_random_knowledge',
    'get_recent_chat', 'get_relevant_facts', 'get_session_start', 'get_session_stats', 'get_stream',
    'get_tagged_answers', 'get_total_stats', 'get_user_interactions', 'get_user_messages',
    'get_user_stats', 'has_chatted', 'init_db', 'invalidate_knowledge_cache', 'last_chat_time',
    'oldest_bot_use_age', 'record_bot_use', 'reopen_stream', 'save_bot_interaction',
    'save_chat_message', 'save_stream', 'search_context', 'transaction', 'vacuum_db',
]
