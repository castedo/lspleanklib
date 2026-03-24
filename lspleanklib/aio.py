"""
Asynchronous IO
"""

import asyncio, io, os, typing
from asyncio import AbstractEventLoop, Future
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO


class MinimalWriter(typing.Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...
    def is_closing(self) -> bool: ...


class MinimalReader(typing.Protocol):
    async def readuntil(self) -> bytes: ...  # read until and return with '\n'
    async def readexactly(self, n: int) -> bytes: ...
    async def read(self) -> bytes: ...
    def at_eof(self) -> bool: ...


class DuplexStream:
    def __init__(self, ain: MinimalReader, aout: MinimalWriter):
        self.ain = ain
        self.aout = aout


class WriterFileAdapter(MinimalWriter):
    def __init__(self, fout: BinaryIO, loop: AbstractEventLoop):
        self._fout = fout
        self._loop = loop
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="blocking_write")
        self._buf: list[bytes] = []
        self._closing: Future[None] | None = None

    def write(self, data: bytes) -> None:
        if self._closing is not None:
            raise ValueError('write to closed file')
        if data:
            self._buf.append(data)

    async def drain(self) -> None:
        if self._buf:
            todo = tuple(self._buf)
            self._buf.clear()
            await self._loop.run_in_executor(self._pool, self._drain, todo)

    def _drain(self, todo: tuple[bytes, ...]) -> None:
        for data in todo:
            self._fout.write(data)
        self._fout.flush()

    def close(self) -> None:
        if self._closing is None:
            self._closing = self._loop.run_in_executor(self._pool, self._fout.close)

    async def wait_closed(self) -> None:
        if self._closing is None:
            raise ValueError('file not closed')
        await self._closing

    def is_closing(self) -> bool:
        return self._closing is not None


class ReadFilePump:
    def __init__(self, fd: int, loop: AbstractEventLoop) -> None:
        self._fd = fd
        self._loop = loop
        self.stream = asyncio.StreamReader(loop=loop)

    def run(self) -> None:
        while True:
            try:
                data = os.read(self._fd, io.DEFAULT_BUFFER_SIZE)
            except KeyboardInterrupt as ex:
                nex = IOError('file read interrupted')
                nex.__cause__ = ex
                self._loop.call_soon_threadsafe(self.stream.set_exception, nex)
                return
            if data:
                self._loop.call_soon_threadsafe(self.stream.feed_data, data)
            else:
                self._loop.call_soon_threadsafe(self.stream.feed_eof)
                return
