"""SQLite storage of the bot, one module per concern. src/core/database.py re-exports them.

    connection.py    the shared connection, backup, vacuum
    schema.py        init_db(): schema and migrations of every table
    chat.py          chat_messages
    interactions.py  bot_interactions
    knowledge.py     knowledge and facts, FTS search, the random sample
    quota.py         bot_uses: quotas and per-stream limits
    streams.py       streams
"""
