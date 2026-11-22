"""!project: the streamer sets what the stream is working on, anyone asks."""
import re

import pytest

from fakes import FakeBot, make_chatter, make_message, chat_component
from src.core import content
from src.core.config import Follow
from src.core.database import get_state
from src.twitch.local import project


def _texts(**templates: str) -> None:
    path = content.CONTENT_PATH
    text = path.read_text(encoding='utf-8')
    for key, value in templates.items():
        text = re.sub(rf'(### {key}\n).*', lambda m, v=value: m.group(1) + v, text)
    path.write_text(text, encoding='utf-8')


@pytest.fixture
def component(monkeypatch):
    monkeypatch.setattr(Follow, 'REQUIRED', True)
    return chat_component(FakeBot())


async def _say(component, text: str, chatter=None):
    message = make_message(text, chatter or make_chatter('gop'))
    await component.event_message(message)
    return message


async def test_nothing_set_yet(db, component):
    message = await _say(component, '!project')
    message.respond.assert_awaited_once_with('texts.project_none')


async def test_the_streamer_sets_it_in_its_own_case_and_anyone_reads_it(db, component):
    _texts(project='{user}: {project}', project_set='ок {project}')
    streamer = make_chatter('exitfound', broadcaster=True)
    message = await _say(component, '!project title  Делаем   ИИ для NPC ', streamer)
    message.respond.assert_awaited_once_with('ок Делаем ИИ для NPC')
    assert await get_state(project.STATE_KEY) == 'Делаем ИИ для NPC'
    # Read without a follow, like every channel command
    component.bot.follows = False
    message = await _say(component, '!project')
    message.respond.assert_awaited_once_with('gop: Делаем ИИ для NPC')


async def test_a_viewer_cannot_set_it_and_gets_no_answer(db, component):
    for chatter in (make_chatter('gop'), make_chatter('mod', moderator=True)):
        message = await _say(component, '!project title взлом', chatter)
        message.respond.assert_not_awaited()
    assert await get_state(project.STATE_KEY) is None


async def test_an_empty_or_too_long_title_is_refused(db, component):
    streamer = make_chatter('exitfound', broadcaster=True)
    message = await _say(component, '!project title', streamer)
    message.respond.assert_awaited_once_with('texts.project_usage')
    message = await _say(component, '!project title ' + 'x' * (project.TEXT_MAX + 1), streamer)
    message.respond.assert_awaited_once_with('texts.project_too_long')
    assert await get_state(project.STATE_KEY) is None


def test_project_is_listed_by_help_channel_but_title_is_not(component):
    listing = component._channel.listing()
    assert '!project' in listing
    assert '!project title' not in listing


async def test_a_new_project_reopens_the_shared_pause(db, component):
    """The last !project answer in chat is stale after a title: a viewer asking right away gets the new one."""
    await _say(component, '!project')
    streamer = make_chatter('exitfound', broadcaster=True)
    await _say(component, '!project title новое', streamer)
    message = await _say(component, '!project', make_chatter('late'))
    message.respond.assert_awaited_once()
