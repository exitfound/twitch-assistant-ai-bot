"""Twitch features of the bot that do not use Gemini.

    commands.py    !help, !bot, !stat
    channel.py     the channel's own commands (!tg …) from CONTENT.md, !channel
    clip.py        !clip – a clip of the stream's last seconds
    schedule.py    !schedule – the nearest stream from the channel's schedule on Twitch
    project.py     !project – what the stream is working on, set by the streamer
    lastseen.py    !lastseen – when a viewer last wrote in chat
    follow.py      reply to a new follow
    emote_spam.py  a batch of emotes in chat once per interval
    film/          !film – the cinema hall's session kept by its gate, and the chat bridge to the hall
    mascot/        the OBS overlay's mood from chat activity, served over SSE

A small command without generation goes here as a module of its own. A feature with its
own tables, texts and background tasks gets a subpackage right here, like mascot/;
one that grows into a service of its own moves out to src/<service>/, like the game (src/roll/).
"""
