"""The «залупа стрима» (session loser) game: !roll, channel-points rewards, curse-lift
announcement.

    rules.py       the rules as pure functions: a throw, a curse, minutes left, a nick
    game.py        mechanics – the only place that changes rolls
    storage.py     queries on rolls, rewards, roll_actions and roll_perks
    perks.py       perks from the previous stream: announce, start the countdown
    command.py     !roll
    texts.py       shared message pieces: китежанин (champion), curse, reward titles
    redemption.py  apply a redemption and say it in chat
    rewards.py     Twitch: reward creation, subscription, statuses, pause
    announce.py    background announcement of a curse lift
"""
