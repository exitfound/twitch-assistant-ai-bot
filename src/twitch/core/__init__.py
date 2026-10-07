"""The Twitch skeleton: command registry and dispatcher, the gate, limits, chat sockets,
tokens, the stream tracker, follower checks, viewer tiers, replies in chat.

Twitch features depend on it; component.py is the one module that imports every feature,
since it registers their commands.
"""
