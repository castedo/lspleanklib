import pytest

import asyncio, contextlib
from collections.abc import Awaitable

from lspleanklib.jsonrpc import (
    JsonRpcDuplexConnection,
    MethodCall,
    RpcInterface,
    Response,
    read_message,
    write_message,
)

from util import aio_xpipe


class NotImplementedService(RpcInterface):
    async def notify(self, mc: MethodCall) -> None:
        raise NotImplementedError

    async def request(self, mc: MethodCall, fix_id: str | None) -> Awaitable[Response]:
        raise NotImplementedError

    def release(self) -> None:
        pass

class NullService(RpcInterface):
    async def notify(self, mc: MethodCall) -> None:
        pass

    async def request(self, mc: MethodCall, fix_id: str | None) -> Awaitable[Response]:
        async def trivial() -> Response:
            return Response(None)
        return trivial()

    def release(self) -> None:
        pass

async def test_response():
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcDuplexConnection(local)
            ta = tg.create_task(con.run(NotImplementedService()))
            tbd = await con.remote.request(MethodCall("do/thing", None), None)
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "method": "do/thing", "params": None}
            await write_message(remote.aout, {"jsonrpc":"2.0", "id": 1, "result": "nothing"})
            assert await tbd == Response("nothing")
            remote.aout.close()
    assert ta.result() == True


async def test_response_cancelled():
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcDuplexConnection(local)
            ta = tg.create_task(con.run(NotImplementedService()))
            tbd = await con.remote.request(MethodCall("do/thing", None), None)
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "method": "do/thing", "params": None}
            remote.aout.close()
            cancelled = None
            try:
                await tbd
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            assert cancelled == True
    assert ta.result() == True


async def test_simple_serve():
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcDuplexConnection(local)
            ta = tg.create_task(con.run(NullService()))
            await write_message(remote.aout, {"jsonrpc":"2.0", "id": 1, "method": "nothing"})
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "result": None}
            remote.aout.close()
    assert ta.result() == True
