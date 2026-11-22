"""The «залупа стрима» (session loser) game service: !roll, channel-points rewards, perks,
curse-lift announcements, the OBS overlay. It talks to Twitch only through the bot.

    rules.py       the rules as pure functions: a throw, a curse, minutes left
    game.py        mechanics – the only place that changes rolls
    storage.py     queries on rolls, roll_actions, roll_perks, roll_throws, roll_tiers and roll_chokes
    port.py        RollPort: what the game needs from the bot
    request.py     a game command as the game sees it, and what the bot's gate must give back
    service.py     the entry points: commands, chat and stream events, redemptions
    specs.py       the channel-points rewards as the game defines them, for the bot to create
    perks.py       perks from the previous stream: announce, start the countdown
    tiers.py       the viewer's status in the stream, from chat, for the rewards
    command.py     !roll, !rollstat
    board.py       !rollboard: the month's top threes, the last month's results at the month's first stream
    texts.py       shared message pieces: китежанин (champion), curse, reward titles
    redemption.py  apply a redemption and say it in chat
    announce.py    background announcement of a curse lift
    choke.py       background steps of the choking flask
    overlay.py     the game on the stream: events for the OBS page overlay.html
"""
