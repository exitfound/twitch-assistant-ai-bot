"""The cinema hall, the bot's side: its session kept by the hall's gate, and the chat bridge.

    hall.py     !film – the link, code and film title; the session opened, renamed, closed;
                the lease heartbeat, the reminder, closing after the stream and on shutdown
    bridge.py   the chat bridge Twitch → hall while a session is open

The hall's server (Owncast, the gate) is kinozal/ in the repository root, deployed to its own host.
"""
