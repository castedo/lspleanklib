import asyncio, contextlib, os
from typing import BinaryIO
from concurrent.futures import ThreadPoolExecutor

from lspleanklib.aio import DuplexStream, MinimalReader, ReadFilePump, WriterFileAdapter


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
