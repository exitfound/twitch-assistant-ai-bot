"""The voice as a service: the bot forwards answers over HTTP, the service publishes them to its speaker."""
import asyncio
import contextlib

from aiohttp.test_utils import TestServer

from src.core import speech
from src.discord import service


async def test_an_answer_goes_from_the_bot_to_the_voice_services_speaker():
    """The forwarder's send reaches the service, which publishes it to the speaker of its process.
    The forwarder never runs in the voice process: there it would hear its own answers again."""
    server = TestServer(service.app())
    await server.start_server()
    heard = []
    remove = speech.listen(lambda text, source: heard.append((text, source)))
    forwarder = speech.Forwarder(str(server.make_url('')))
    try:
        await forwarder._send('@gop привет', 'twitch')
    finally:
        remove()
        await forwarder._client.close()
        await server.close()
    assert heard == [('@gop привет', 'twitch')]


async def test_a_voice_that_is_down_never_holds_up_the_chat(caplog):
    forwarder = speech.Forwarder('http://127.0.0.1:9')
    task = asyncio.create_task(forwarder.loop())
    try:
        await asyncio.sleep(0)
        for i in range(speech.FORWARD_QUEUE + 5):
            speech.say(f'реплика {i}', 'twitch')       # never blocks, the oldest are dropped
        await asyncio.wait_for(forwarder._queue.join(), 10)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert caplog.text.count('Голос недоступен') == 1
    assert speech._listeners == [] or forwarder.submit not in speech._listeners
