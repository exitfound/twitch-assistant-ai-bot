"""The bot's side of the game service (src/roll/): it carries the game's commands, chat and
stream events to it, and keeps the Twitch half of the channel-points rewards.

    client.py      RollLink – what the bot asks the game and tells it; RollUnavailable
    rewards.py     the rewards on Twitch: creation from the game's specs, subscription, statuses, pause
"""
