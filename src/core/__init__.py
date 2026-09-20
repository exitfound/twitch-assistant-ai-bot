"""Bot skeleton: config, texts, DB, logging, utilities, command registry, chat dispatcher.

No features live here. Features sit next door (src/gemini, src/local) and depend
on core, not the other way round. There is one exception: the dispatcher component.py
registers the commands of every feature.
"""
