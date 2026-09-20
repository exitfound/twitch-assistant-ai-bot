"""The bot's long-term memory of chatters, written by Gemini after every conversation.

A conversation is chat between two long silences – on this channel, a stream.

    storage.py  queries on chronicles, chatter_events, chatter_profiles, memory_state
    build.py    a conversation's chronicle and profile updates; the background check

The whole history is built once from the CLI: bot.py --build-memory (src/cli/memory.py).
"""
