"""
Generic server code
"""

from __future__ import annotations
import abc, asyncio, sys, threading, typing
from asyncio import AbstractEventLoop, TaskGroup, subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Sequence

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .jsonrpc import (
    JsonRpcDuplexChannel,
    RpcDuplexChannel,
    RpcInterface,
)
from .util import log


class LocalChannelFactory(typing.Protocol):
    async def anew(self, work_dir: Path) -> RpcDuplexChannel: ...


class RpcSubprocess(RpcDuplexChannel):
    def __init__(self, work_dir: Path, proc: subprocess.Process):
        self._work_dir = work_dir
        assert proc.stdin and proc.stdout
        self._proc = proc
        aio = DuplexStream(proc.stdout, proc.stdin)
        self._sub_con = JsonRpcDuplexChannel(aio, 'subproc')

    @property
    def proxy(self) -> RpcInterface:
        return self._sub_con.proxy

    async def pump(self) -> None:
        log.debug(f"Subprocess {self._proc.pid} for {self._work_dir}")
        await self._sub_con.pump()
        await self._proc.communicate()
        if self._proc.returncode != 0:
            msg = "Subprocess exit return code {} for {}"
            raise RuntimeError(msg.format(self._proc.returncode, self._work_dir))

    def handle(self, client: RpcInterface) -> None:
        self._sub_con.handle(client)

    @staticmethod
    async def anew(cmd: Sequence[str], work_dir: Path) -> RpcSubprocess:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=work_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        return RpcSubprocess(work_dir, proc)


class RpcSubprocessFactory(LocalChannelFactory):
    def __init__(self, lsp_cmd: list[str]):
        self._lsp_cmd = lsp_cmd

    async def anew(self, work_dir: Path) -> RpcDuplexChannel:
        return await RpcSubprocess.anew(self._lsp_cmd, work_dir)


class LspServer(RpcInterface, typing.Protocol):
    def is_initialized(self) -> bool: ...


class LspService:
    @abc.abstractmethod
    def start(self, client: RpcInterface, tg: TaskGroup) -> LspServer: ...

    async def run(self, client_chan: JsonRpcDuplexChannel) -> bool:
        async with TaskGroup() as tg:
            server = self.start(client_chan.proxy, tg)
            client_chan.handle(server)
            tg.create_task(client_chan.pump())
        return server.is_initialized()

    async def amain(self, stdio: DuplexStream) -> int:
        try:
            ok = await self.run(JsonRpcDuplexChannel(stdio, 'stdio'))
        except Exception as ex:
            log.exception(ex)
            return 1
        if not ok:
            log.warning("LSP server never initialized")
            return 1
        return 0


class AsyncMainLoopThread(threading.Thread):
    def __init__(self, loop: AbstractEventLoop, amain: Awaitable[int]):
        super().__init__(name=self.__class__.__name__)
        self._loop = loop
        self._amain = amain
        self.retcode: int | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self.retcode = self._loop.run_until_complete(self._amain)


def async_stdio_main(amain: Callable[[DuplexStream], Awaitable[int]]) -> int:
    loop = asyncio.new_event_loop()
    pump = ReadFilePump(sys.stdin.fileno(), loop)
    stdio = DuplexStream(pump.stream, WriterFileAdapter(sys.stdout.buffer, loop))
    amain_thread = AsyncMainLoopThread(loop, amain(stdio))
    amain_thread.start()
    pump.run()
    try:
        amain_thread.join()
    except KeyboardInterrupt as ex:
        log.exception(ex)
    if amain_thread.retcode is None:
        log.debug('Async main loop thread did not complete properly')
        return 1
    return amain_thread.retcode
