"""Bot features that do not use Gemini.

    commands.py    !help-bot, !stat
    clip.py        !clip – a clip of the stream's last seconds
    follow.py      reply to a new follow
    emote_spam.py  a batch of emotes in chat once per interval
    roll/          the «залупа стрима» (session loser) game: !roll, channel-points rewards,
                   announcements

A small command without generation goes here as a module of its own. A feature with its
own tables, texts and background tasks gets a subpackage right here, like roll/.
"""
