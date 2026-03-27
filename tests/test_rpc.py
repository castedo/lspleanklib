import pytest

import asyncio
from collections.abc import Awaitable

from lspleanklib.jsonrpc import (
    JsonRpcChannel,
    JsonRpcMsgStream,
    MethodCall,
    Response,
    RpcInterface,
    read_message,
    write_message,
)

from util import aio_xpipe


class NotImplementedClient(RpcInterface):
    async def notify(self, mc: MethodCall) -> None:
        raise NotImplementedError

    async def request(self, mc: MethodCall, fix_id: str | None = None) -> Awaitable[Response]:
        raise NotImplementedError

    async def close(self) -> None:
        pass

async def test_response() -> None:
    loop = asyncio.get_running_loop()
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcChannel(JsonRpcMsgStream(local, 'local'), loop)
            tg.create_task(con.pump(NotImplementedClient()))
            tbd = await con.proxy.request(MethodCall("do/thing", None), None)
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "method": "do/thing"}
            await write_message(remote.aout, {"jsonrpc":"2.0", "id": 1, "result": "nothing"})
            assert await tbd == Response("nothing")
            remote.aout.close()


async def test_response_cancelled() -> None:
    loop = asyncio.get_running_loop()
    async with aio_xpipe() as (local, remote):
        async with asyncio.TaskGroup() as tg:
            con = JsonRpcChannel(JsonRpcMsgStream(local, 'local'), loop)
            tg.create_task(con.pump(NotImplementedClient()))
            tbd = await con.proxy.request(MethodCall("do/thing", None), None)
            msg = await read_message(remote.ain)
            assert msg == {"jsonrpc":"2.0", "id": 1, "method": "do/thing"}
            remote.aout.close()
            cancelled = None
            try:
                await tbd
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            assert cancelled == True


async def test_simple_serve() -> None:
    with pytest.warns(UserWarning):
        loop = asyncio.get_running_loop()
        async with aio_xpipe() as (local, remote):
            async with asyncio.TaskGroup() as tg:
                con = JsonRpcChannel(JsonRpcMsgStream(local, 'local'), loop)
                tg.create_task(con.pump())
                await write_message(remote.aout, {"jsonrpc":"2.0", "id": 1, "method": "nothing"})
                msg = await read_message(remote.ain)
                assert msg == {"jsonrpc":"2.0", "id": 1,
                    "error": {"code": -32601, "message": "MethodNotFound", "data": None}
                }
                remote.aout.close()
