"""Twitch features of the bot that do not use Gemini.

    commands.py    !help, !bot, !stat
    channel.py     the channel's own commands (!tg …) from CONTENT.md, !channel
    clip.py        !clip – a clip of the stream's last seconds
    follow.py      reply to a new follow
    emote_spam.py  a batch of emotes in chat once per interval
    roll/          the «залупа стрима» (session loser) game: !roll, channel-points rewards,
                   announcements
    mascot/        the OBS overlay's mood from chat activity, served over SSE

A small command without generation goes here as a module of its own. A feature with its
own tables, texts and background tasks gets a subpackage right here, like roll/.
"""
