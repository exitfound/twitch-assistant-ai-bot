"""Calls between services: what a failure says about whether the work may have been done."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.core.service import ServiceClient, ServiceUnavailable


async def _server(handler) -> TestServer:
    app = web.Application()
    app.router.add_post('/x', handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def _failure(server_or_url, *, seconds: float = 5) -> ServiceUnavailable:
    url = server_or_url if isinstance(server_or_url, str) else str(server_or_url.make_url(''))
    client = ServiceClient('игра', url)
    try:
        with pytest.raises(ServiceUnavailable) as caught:
            await client.call('POST', '/x', {}, seconds=seconds)
    finally:
        await client.close()
    return caught.value


async def test_a_refused_connection_sent_nothing():
    failure = await _failure('http://127.0.0.1:9')
    assert not failure.maybe_done
    assert 'Cannot connect' in str(failure)


@pytest.mark.parametrize(('status', 'maybe_done'), [(500, True), (503, True), (404, False), (403, False)])
async def test_a_server_error_may_follow_done_work_a_refusal_never_does(status, maybe_done):
    async def answer(request):
        return web.Response(status=status)
    server = await _server(answer)
    try:
        assert (await _failure(server)).maybe_done is maybe_done
    finally:
        await server.close()


async def test_a_slow_answer_may_have_done_the_work():
    async def slow(request):
        await asyncio.sleep(2)
        return web.json_response({})
    server = await _server(slow)
    try:
        assert (await _failure(server, seconds=0.2)).maybe_done
    finally:
        await server.close()


async def test_a_connection_dropped_after_the_request_may_have_done_the_work():
    async def drop(request):
        await request.read()
        request.transport.close()
        await asyncio.sleep(1)
        return web.json_response({})
    server = await _server(drop)
    try:
        failure = await _failure(server)
    finally:
        await server.close()
    assert failure.maybe_done
    assert str(failure).split(': ', 1)[1]          # a reason, never an empty one
