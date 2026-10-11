"""The bot's long-term memory of chatters, written by Gemini after every conversation.

A conversation is chat between two long silences – on this channel, a stream.

    storage.py  queries on chronicles, chatter_events, chatter_profiles, chatter_facts,
                memory_state; the facts picked for one answer
    build.py    a conversation's chronicle and dossier updates; the background check

The whole history is built once from the CLI: bot.py --build-memory (src/cli/memory.py).
"""
