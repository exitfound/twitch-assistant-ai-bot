"""The voice service (voice.py): everything tied to Discord.

    main.py     the process: the Discord client and the API the bot speaks through
    service.py  the API: /speech – an answer that reached chat, published to the speaker here
    bot.py      the discord.py client, kept up by DiscordService: the voice channel by
                !join / !leave, the voice by !voice, !help – in the text channel
    local/      Discord features without Gemini: voice/ – answers spoken in the voice channel

Shared code (DB, config, CONTENT.md, the Gemini brain) stays in src/core, src/gemini and
never imports from here; the bot reaches this service only over its API.
"""
