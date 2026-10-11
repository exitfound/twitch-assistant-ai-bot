"""Proactive remarks: what the model is shown – the pulse, the thread, the target's dossier."""
import time
from unittest.mock import AsyncMock

from fakes import FakeBot
from src.core.activity import ChatWatch
from src.core.database import save_chat_message
from src.gemini import pulse
from src.gemini.memory import storage
from src.twitch.gemini import proactive


async def _remark(monkeypatch, target_roll: float) -> str:
    bot = FakeBot(session_id='s')
    await save_chat_message('s', 'gop', 'опять варик')
    await storage.apply_dossier(storage.Profile('gop', 'шапка gop', [], 'k', 1), [('interest', 'фанат варика')],
                                [], [], 40)
    monkeypatch.setattr(pulse, '_pulse', ('s', time.monotonic(), 'чат обсуждает варик'))
    monkeypatch.setattr(proactive.random, 'random', lambda: target_roll)
    generate = AsyncMock(return_value='реплика')
    monkeypatch.setattr(proactive, 'generate', generate)
    await proactive._send_proactive(bot, ChatWatch())
    bot.send_chat_message.assert_awaited_once()
    return generate.await_args.args[0]


async def test_a_remark_about_someone_gets_their_dossier(db, monkeypatch):
    prompt = await _remark(monkeypatch, 0.0)
    assert 'чат обсуждает варик' in prompt and 'опять варик' in prompt
    assert '[labels.dossier]' in prompt and 'фанат варика' in prompt
    assert 'prompts.proactive_user' in prompt


async def test_a_general_remark_goes_by_the_pulse(db, monkeypatch):
    prompt = await _remark(monkeypatch, 0.99)
    assert 'чат обсуждает варик' in prompt
    assert '[labels.dossier]' not in prompt
    assert 'prompts.proactive_general' in prompt
