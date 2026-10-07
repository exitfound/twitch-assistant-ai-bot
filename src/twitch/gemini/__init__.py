"""Twitch handlers of generated answers; the generation itself lives in src/gemini.

    commands.py   !ask, !summary, !who, !versus and free text addressed to the bot
    responder.py  response pipeline: cleanup, stop-list, CAPS, emote, sending
    picture.py    the !ascii handler: download, draw, show, caption
    proactive.py  the bot's own remarks once per interval

A new command that generates text goes here, registered with kind=Kind.GEMINI.
"""
