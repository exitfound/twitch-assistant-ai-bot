"""The cinema hall's gate: the entry, the session API and the chat bridge (docs/KINOZAL.md).

Caddy asks /_gate/auth before every request to Owncast. A viewer gets in with Twitch (followers and
subscribers of TWITCH_CHANNEL) plus the session's code. The bot opens, renames and closes the session
through /_gate/api/session, and each of its requests renews a lease. Twitch lines come in through
/_gate/api/chat. Hall lines go to Twitch as the viewer, with the viewer's token, which stays in memory
for the session only.

Modules: main (the app), config, state, storage, security, access, session, upstream, twitch, owncast,
twins, voices, look, moderation, bridge, pages, texts, chat_markup; routers: entry, login, hall, api, body.
"""
