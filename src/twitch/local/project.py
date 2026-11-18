"""!project – what the stream is working on, set by the streamer from chat.

`!project title <text>` is the streamer's; `!project` answers anyone, without a follow.
The text lives in bot_state: it survives restarts and holds until the next title, since
a project usually spans several streams.
"""
from src.core.content import Content
from src.core.database import get_state, set_state
from src.twitch.core.commands import PUBLIC_SCOPE, CommandContext
from src.twitch.core.replies import reply

TRIGGER = '!project'
TITLE_TRIGGER = '!project title'
STATE_KEY = 'channel_project'
# Leaves room for the nick and the words around it within Twitch's 500 characters
TEXT_MAX = 300


async def handle_project(ctx: CommandContext) -> None:
    project = await get_state(STATE_KEY)
    if project:
        await reply(ctx.message, Content.text('project', user=ctx.user, project=project))
    else:
        await reply(ctx.message, Content.text('project_none', user=ctx.user))


async def handle_project_title(ctx: CommandContext) -> None:
    # The original case: «Делаем ИИ для NPC» must not reach the chat lowercased
    project = ' '.join(ctx.original_args.split())
    if not project:
        await reply(ctx.message, Content.text('project_usage', user=ctx.user, command=TITLE_TRIGGER))
        return
    if len(project) > TEXT_MAX:
        await reply(ctx.message, Content.text('project_too_long', user=ctx.user, max=TEXT_MAX))
        return
    await set_state(STATE_KEY, project)
    # The last !project answer in chat is now stale: the shared pause must not keep the new one back
    ctx.bot.clear_cooldown(TRIGGER, PUBLIC_SCOPE)
    await reply(ctx.message, Content.text('project_set', user=ctx.user, project=project))
