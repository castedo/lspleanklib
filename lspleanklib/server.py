"""
Generic server code
"""

from __future__ import annotations
import abc, asyncio, os, sys, threading, typing
from asyncio import AbstractEventLoop, TaskGroup, subprocess
from collections.abc import Awaitable
from pathlib import Path
from warnings import warn
from typing import Sequence

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .cli import version
from .jsonrpc import (
    ErrorCodes,
    JsonRpcChannel,
    MethodCall,
    Response,
    RpcChannel,
    RpcInterface,
    RpcSession,
    future_error,
)
from .util import LspObject, awaitable, get_obj, log


LSP_CLIENT_NAME = "lspleank"
LSP_SERVER_NAME = "lspleank"


class RpcDirChannelFactory(typing.Protocol):
    async def anew(self, work_root: Path, loop: AbstractEventLoop) -> RpcChannel: ...


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


class LspInitializer(typing.Protocol):
    async def on_initialize(self, init_params: LspObject) -> Response: ...
    async def do_initialized(self) -> RpcInterface | None: ...
    def close(self) -> None: ...


class LspServer(RpcInterface):
    def __init__(self, initializer: LspInitializer) -> None:
        self._initialized: RpcInterface | None = None
        self._initializer = initializer

    def is_initialized(self) -> bool:
        return self._initialized is not None

    def close(self) -> None:
        if self._initialized:
            self._initialized.close()
        self._initializer.close()

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if mc.method == 'initialize':
            init_params = mc.params if isinstance(mc.params, dict) else {}
            if self._initialized is None:
                response = await self._initializer.on_initialize(init_params)
                return awaitable(response)
            else:
                return future_error(ErrorCodes.InvalidRequest)
        elif self._initialized:
            return await self._initialized.request(mc, fix_id)
        else:
            return future_error(ErrorCodes.ServerNotInitialized)

    async def notify(self, mc: MethodCall) -> None:
        got = f"Got '{mc.method}' notification"
        if mc.method == 'initialized':
            if self._initialized is None:
                initialized = await self._initializer.do_initialized()
                if initialized:
                    self._initialized = initialized
                else:
                    warn(got + " without 'initialize' request success")
            else:
                warn(got + " when already initialized")
        elif self._initialized:
            await self._initialized.notify(mc)
        else:
            warn(got + " when not initialized")


class RpcSubprocess(RpcChannel):
    def __init__(
        self, proc: subprocess.Process, work_dir: Path, loop: AbstractEventLoop
    ):
        self._work_dir = work_dir
        assert proc.stdin and proc.stdout
        self._proc = proc
        aio = DuplexStream(proc.stdout, proc.stdin)
        self._sub_con = JsonRpcChannel(aio, loop, 'subproc')

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
    async def anew(
        cmd: Sequence[str], work_dir: Path, loop: AbstractEventLoop
    ) -> RpcSubprocess:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=work_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        return RpcSubprocess(proc, work_dir, loop)


class RpcSubprocessFactory(RpcDirChannelFactory):
    def __init__(self, lsp_cmd: list[str]):
        self._lsp_cmd = lsp_cmd

    async def anew(self, work_dir: Path, loop: AbstractEventLoop) -> RpcChannel:
        return await RpcSubprocess.anew(self._lsp_cmd, work_dir, loop)


def get_socket_path(work_root: Path) -> Path | None:
    if not hasattr(asyncio, 'open_unix_connection'):
        return None
    dir_socket = work_root / ".lspleank.sock"
    if not dir_socket.exists():
        xdg_dir = os.environ.get("XDG_RUNTIME_DIR")
        if xdg_dir is not None:
            dir_socket = Path(xdg_dir) / "lspleank.sock"
    return dir_socket if dir_socket.exists() else None


class RpcSocketFactory(RpcDirChannelFactory):
    def __init__(self, default: RpcDirChannelFactory):
        self._default = default

    async def anew(self, work_root: Path, loop: AbstractEventLoop) -> RpcChannel:
        sock_path = get_socket_path(work_root)
        if sock_path is None:
            return await self._default.anew(work_root, loop)
        else:
            assert loop == asyncio.get_running_loop()
            (reader, writer) = await asyncio.open_unix_connection(sock_path)
            aio = DuplexStream(reader, writer)
            return JsonRpcChannel(aio, loop, 'socket')


class LspSession(RpcSession, typing.Protocol):
    def was_initialized(self) -> bool: ...


class ChannelRpcSession(RpcSession):
    def __init__(self, channel: RpcChannel):
        self._channel = channel

    def start_server(self, client: RpcInterface, tg: TaskGroup) -> RpcInterface:
        tg.create_task(self._channel.pump(client))
        return self._channel.proxy


class RpcSessionFactory(typing.Protocol):
    async def anew(self, work_dir: Path) -> RpcSession: ...


class ChannelRpcSessionFactory(RpcSessionFactory):
    def __init__(self, factory: RpcDirChannelFactory, loop: AbstractEventLoop):
        self._factory = factory
        self._loop = loop

    async def anew(self, work_dir: Path) -> RpcSession:
        channel = await self._factory.anew(work_dir, self._loop)
        return ChannelRpcSession(channel)


class AsyncProgram(typing.Protocol):
    async def amain(self, stdio: DuplexStream, loop: AbstractEventLoop) -> int: ...


async def lsp_server_loop(session: LspSession, client: JsonRpcChannel) -> bool:
    async with TaskGroup() as tg:
        server = session.start_server(client.proxy, tg)
        tg.create_task(client.pump(server))
    return session.was_initialized()


class LspProgram:
    @abc.abstractmethod
    async def aget_session(self, loop: AbstractEventLoop) -> LspSession: ...

    async def amain(self, stdio: DuplexStream, loop: AbstractEventLoop) -> int:
        try:
            client_chan = JsonRpcChannel(stdio, loop, 'stdio')
            session = await self.aget_session(loop)
            ok = await lsp_server_loop(session, client_chan)
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
