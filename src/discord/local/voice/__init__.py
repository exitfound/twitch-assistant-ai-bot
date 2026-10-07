"""The bot's answers spoken in the Discord voice channel.

    text.py     an answer → text the Russian TTS model reads well: nicks, emotes, numbers
    tts.py      the TTS server client: raw speech streamed back as it is generated
    audio.py    24 kHz mono → 48 kHz stereo, and the source discord.py plays from
    speaker.py  the queue: one answer at a time, speech gathered before playback starts
"""
