"""Everything tied to Twitch: the bot, its chat dispatcher and its features.

    bot.py   the twitchio Bot: lifecycle, EventSub, sending to chat, the stream state
    core/    the Twitch skeleton: command registry, dispatcher, gate, sockets, tokens, stream
    gemini/  Twitch handlers of the generated answers: !ask, !who, !summary, !ascii, proactive remarks
    local/   Twitch features without Gemini: !roll, !clip, follows, emotes, the mascot feed

Shared code (DB, config, CONTENT.md, the Gemini brain, CLI) stays in src/core, src/gemini, src/cli
and never imports from here.
"""
