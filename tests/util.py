import asyncio, contextlib, os
from asyncio import Future
from pathlib import Path
from typing import BinaryIO, Awaitable
from concurrent.futures import ThreadPoolExecutor

from lspleanklib.aio import DuplexStream, MinimalReader, ReadFilePump, WriterFileAdapter
from lspleanklib.jsonrpc import LspAny, MethodCall, Response, RpcInterface


async def asyncio_pipe_stream_reader(pipe: BinaryIO, loop) -> MinimalReader:
    ret = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(ret)
    await loop.connect_read_pipe(lambda: protocol, pipe)
    return ret


async def pump_pipe_stream_reader(pipe: BinaryIO, loop) -> MinimalReader:
    pump = ReadFilePump(pipe.fileno(), loop)
    pool = ThreadPoolExecutor(1, thread_name_prefix="blocking_read")
    loop.run_in_executor(pool, pump.run)
    return pump.stream


#pipe_stream_reader = asyncio_pipe_stream_reader
pipe_stream_reader = pump_pipe_stream_reader


@contextlib.asynccontextmanager
async def aio_xpipe():
    loop = asyncio.get_running_loop()
    outer_r, inner_w = os.pipe()
    inner_r, outer_w = os.pipe()
    with (
        os.fdopen(outer_r, 'rb') as o_r,
        os.fdopen(inner_w, 'wb') as i_w,
        os.fdopen(inner_r, 'rb') as i_r,
        os.fdopen(outer_w, 'wb') as o_w,
    ):
        outer_reader = await pipe_stream_reader(o_r, loop)
        inner_reader = await pipe_stream_reader(i_r, loop)
        outer = DuplexStream(outer_reader, WriterFileAdapter(o_w, loop))
        inner = DuplexStream(inner_reader, WriterFileAdapter(i_w, loop))
        yield outer, inner


def initialize_call(rootPath: Path) -> MethodCall:
    rootUri = rootPath.as_uri()
    return MethodCall("initialize", {
      "workspaceFolders": [{"uri": rootUri, "name": rootPath.name}],
      "clientInfo": {"name": "mock test client"},
    })


class MockClient(RpcInterface):
    def __init__(self) -> None:
        self.notifs: dict[str, Future[LspAny]] = {}

    def future_notif(self, method: str) -> Future[LspAny]:
        f = self.notifs.get(method)
        if f is None:
            f = asyncio.get_running_loop().create_future()
            self.notifs[method] = f
        return f

    async def notify(self, mc: MethodCall) -> None:
        f = self.notifs.pop(mc.method, None)
        if f is not None:
            f.set_result(mc.params)

    async def request(self, mc: MethodCall, fix_id: str | None = None) -> Awaitable[Response]:
        async def trivial() -> Response:
            return Response(None)
        return trivial()
