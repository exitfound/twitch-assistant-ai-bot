"""The shared skeleton: config, texts, DB, logging, utilities, cooldowns, background tasks.

Nothing platform- or feature-specific lives here: the platforms (src/twitch) and the
brain (src/gemini) depend on core, not the other way round.
"""
