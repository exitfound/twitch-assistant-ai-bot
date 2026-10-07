"""The brain: everything that costs a Gemini request, independent of the platform.

    client.py          the client, generate() with retries, make_gen_config()
    context.py         ContextBuilder – assembles a prompt from sections
    ladder.py          the fallback ladder: the same request with less context while it is blocked
    answer_context.py  the context of a free-text answer and its rungs
    output.py          chat limits and the cleanup of an answer: CAPS, markdown, dashes, trimming
    summary.py         the context of !summary
    who.py             the context of !who and !versus
    memory/            long-term memory: session chronicles and chatter profiles
    picture/           !ascii: safe download and braille rendering

The handlers that put an answer into a platform's chat live in that platform's package
(src/twitch/gemini).
"""
