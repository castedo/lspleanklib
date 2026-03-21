import pytest

import asyncio
from collections.abc import Awaitable

from lspleanklib.jsonrpc import (
    JsonRpcDuplexChannel,
    MethodCall,
    Response,
    RpcInterface,
    read_message,
    write_message,
)

from util import aio_xpipe


class NullService(RpcInterface):
    async def notify(self, mc: MethodCall) -> None:
        pass

    async def request(self, mc: MethodCall, fix_id: str | None = None) -> Awaitable[Response]:
        async def trivial() -> Response:
            return Response(None)
        return trivial()

    def close(self) -> None:
        pass

async def test_response() -> None:
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcDuplexChannel(local, 'local')
            ta = tg.create_task(con.pump())
            tbd = await con.proxy.request(MethodCall("do/thing", None), None)
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "method": "do/thing", "params": None}
            await write_message(remote.aout, {"jsonrpc":"2.0", "id": 1, "result": "nothing"})
            assert await tbd == Response("nothing")
            remote.aout.close()


async def test_response_cancelled() -> None:
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcDuplexChannel(local, 'local')
            ta = tg.create_task(con.pump())
            tbd = await con.proxy.request(MethodCall("do/thing", None), None)
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


async def test_simple_serve() -> None:
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcDuplexChannel(local, 'local')
            con.handle(NullService())
            ta = tg.create_task(con.pump())
            await write_message(remote.aout, {"jsonrpc":"2.0", "id": 1, "method": "nothing"})
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "result": None}
            remote.aout.close()
