"""Everything that costs a Gemini request.

    client.py     the client, generate() with retries, make_gen_config()
    context.py    ContextBuilder – assembles a prompt from sections
    responder.py  response pipeline: cleanup, stop-list, CAPS, emote, sending
    commands.py   !ask, !summary, !who, !versus and free text addressed to the bot
    proactive.py  the bot's own remarks once per interval
    memory/       long-term memory: session chronicles and chatter profiles

A new command that generates text goes here, registered with kind=Kind.GEMINI.
"""
