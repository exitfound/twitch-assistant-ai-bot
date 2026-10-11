"""Everything tied to Discord.

    bot.py   the discord.py client, kept up by DiscordService: the voice channel by
             !join / !leave, the voice by !voice, !help – in the text channel
    local/   Discord features without Gemini: voice/ – answers spoken in the voice channel

Shared code (DB, config, CONTENT.md, the Gemini brain) stays in src/core, src/gemini and
never imports from here.
"""
