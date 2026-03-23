"""
Generic server code
"""

from __future__ import annotations
import abc, asyncio, os, sys, threading, typing
from asyncio import AbstractEventLoop, TaskGroup, subprocess
from pathlib import Path
from typing import Sequence

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .cli import version
from .jsonrpc import (
    JsonRpcDuplexChannel,
    MethodCall,
    Response,
    RpcDuplexChannel,
    RpcInterface,
)
from .util import LspObject, get_obj, log


LSP_CLIENT_NAME = "lspleank"
LSP_SERVER_NAME = "lspleank"


class RpcDirChannelFactory(typing.Protocol):
    async def anew(self, work_dir: Path) -> RpcDuplexChannel: ...


def text_doc_caps(init_params: LspObject) -> LspObject:
    caps = get_obj(init_params, 'capabilities')
    return {'textDocument': caps.get('textDocument', {})}


def leank_init_call(work_root: Path, capabilities: LspObject) -> MethodCall:
    return MethodCall(
        'initialize',
        {
            'capabilities': capabilities,
            'clientInfo': {'name': LSP_CLIENT_NAME, 'version': version()},
            'processId': os.getpid(),
            'rootUri': work_root.as_uri(),
        },
    )


def leank_init_response(init_response: Response) -> Response:
    if init_response.error is not None:
        return init_response
    else:
        if isinstance(init_response.result, dict):
            # TODO check and standardize server caps
            server_caps = init_response.result.get('capabilities')
            server_caps = server_caps if isinstance(server_caps, dict) else {}
        else:
            server_caps = {}
    return Response(
        {
            'capabilities': server_caps,
            'serverInfo': {'name': LSP_SERVER_NAME, 'version': version()},
        }
    )


class RpcSubprocess(RpcDuplexChannel):
    def __init__(self, proc: subprocess.Process, work_dir: Path):
        self._work_dir = work_dir
        assert proc.stdin and proc.stdout
        self._proc = proc
        aio = DuplexStream(proc.stdout, proc.stdin)
        self._sub_con = JsonRpcDuplexChannel(aio, 'subproc')

    @property
    def proxy(self) -> RpcInterface:
        return self._sub_con.proxy

    async def pump(self, parent: RpcInterface) -> None:
        log.debug(f"Subprocess {self._proc.pid} for {self._work_dir}")
        await self._sub_con.pump(parent)
        await self._proc.communicate()
        if self._proc.returncode != 0:
            msg = "Subprocess exit return code {} for {}"
            raise RuntimeError(msg.format(self._proc.returncode, self._work_dir))

    @staticmethod
    async def anew(cmd: Sequence[str], work_dir: Path) -> RpcSubprocess:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=work_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        return RpcSubprocess(proc, work_dir)


class RpcSubprocessFactory(RpcDirChannelFactory):
    def __init__(self, lsp_cmd: list[str]):
        self._lsp_cmd = lsp_cmd

    async def anew(self, work_dir: Path) -> RpcDuplexChannel:
        return await RpcSubprocess.anew(self._lsp_cmd, work_dir)


class LspServer(RpcInterface, typing.Protocol):
    def is_initialized(self) -> bool: ...


class AsyncProgram(typing.Protocol):
    async def amain(self, stdio: DuplexStream, loop: AbstractEventLoop) -> int: ...


async def lsp_server_loop(server: LspProgram, client: JsonRpcDuplexChannel) -> bool:
    async with TaskGroup() as tg:
        server_proxy = server.start(client.proxy, tg)
        tg.create_task(client.pump(server_proxy))
    return server_proxy.is_initialized()


class LspProgram:
    @abc.abstractmethod
    def start(self, client: RpcInterface, tg: TaskGroup) -> LspServer: ...

    async def amain(self, stdio: DuplexStream, loop: AbstractEventLoop) -> int:
        try:
            client_chan = JsonRpcDuplexChannel(stdio, 'stdio')
            ok = await lsp_server_loop(self, client_chan)
        except Exception as ex:
            log.exception(ex)
            return 1
        if not ok:
            log.warning("LSP server never initialized")
            return 1
        return 0


class AsyncMainLoopThread(threading.Thread):
    def __init__(
        self, aprog: AsyncProgram, stdio: DuplexStream, loop: AbstractEventLoop
    ):
        super().__init__(name=self.__class__.__name__)
        self._aprog = aprog
        self._stdio = stdio
        self._loop = loop
        self.retcode: int | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self._loop)
        coro = self._aprog.amain(self._stdio, self._loop)
        self.retcode = self._loop.run_until_complete(coro)


def async_stdio_main(aprog: AsyncProgram) -> int:
    loop = asyncio.new_event_loop()
    stdin_pump = ReadFilePump(sys.stdin.fileno(), loop)
    stdio = DuplexStream(stdin_pump.stream, WriterFileAdapter(sys.stdout.buffer, loop))
    amain_thread = AsyncMainLoopThread(aprog, stdio, loop)
    amain_thread.start()
    stdin_pump.run()
    try:
        amain_thread.join()
    except KeyboardInterrupt as ex:
        log.exception(ex)
    if amain_thread.retcode is None:
        log.debug('Async main loop thread did not complete properly')
        return 1
    return amain_thread.retcode
