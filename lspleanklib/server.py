"""
Generic server code
"""

from __future__ import annotations
import abc, asyncio, os, sys, signal, threading, typing
from asyncio import AbstractEventLoop, TaskGroup, subprocess
from collections.abc import Awaitable
from pathlib import Path
from warnings import warn
from typing import Sequence

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .cli import version
from .jsonrpc import (
    ErrorCodes,
    RpcMsgChannel,
    JsonRpcMsgStream,
    MethodCall,
    Response,
    RpcChannel,
    RpcInterface,
    future_error,
    json_rpc_channel,
)
from .util import LspObject, awaitable, get_obj, get_uri_path, log

import socket

OS_WITH_UNIX_DOMAIN_SOCKET_SUPPORT = hasattr(socket, 'AF_UNIX')


LSP_CLIENT_NAME = "lspleank"
LSP_SERVER_NAME = "lspleank"


class RpcDirChannelFactory(typing.Protocol):
    async def anew(self, work_root: Path) -> RpcChannel: ...


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
    async def close_and_wait(self) -> None: ...


class LspServer(RpcInterface):
    def __init__(self, initializer: LspInitializer) -> None:
        self._initialized: RpcInterface | None = None
        self._initializer = initializer

    def was_initialized(self) -> bool:
        return self._initialized is not None

    async def close_and_wait(self) -> None:
        if self._initialized:
            await self._initialized.close_and_wait()
        await self._initializer.close_and_wait()

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
        self, proc: subprocess.Process, work_dir: Path, *, loop: AbstractEventLoop
    ):
        self._work_dir = work_dir
        assert proc.stdin and proc.stdout
        self._proc = proc
        aio = DuplexStream(proc.stdout, proc.stdin)
        self._sub_con = RpcMsgChannel(JsonRpcMsgStream(aio), name='subproc', loop=loop)

    @property
    def proxy(self) -> RpcInterface:
        return self._sub_con.proxy

    async def pump(self, parent: RpcInterface | None = None) -> None:
        log.debug(f"Subprocess {self._proc.pid} for {self._work_dir}")
        await self._sub_con.pump(parent)
        await self._proc.communicate()
        if self._proc.returncode != 0:
            msg = "Subprocess exit return code {} for {}"
            raise RuntimeError(msg.format(self._proc.returncode, self._work_dir))

    @staticmethod
    async def anew(
        cmd: Sequence[str], work_dir: Path, *, loop: AbstractEventLoop
    ) -> RpcSubprocess:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=work_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        return RpcSubprocess(proc, work_dir, loop=loop)


class RpcSubprocessFactory(RpcDirChannelFactory):
    def __init__(self, lsp_cmd: list[str], *, loop: AbstractEventLoop):
        self._lsp_cmd = lsp_cmd
        self._loop = loop

    async def anew(self, work_dir: Path) -> RpcChannel:
        return await RpcSubprocess.anew(self._lsp_cmd, work_dir, loop=self._loop)


def get_user_socket_path() -> Path:
    if not OS_WITH_UNIX_DOMAIN_SOCKET_SUPPORT:
        raise NotImplementedError("This system does not support UNIX sockets")
    xdg_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    return xdg_dir / "lspleank.sock"


def find_socket_path(work_root: Path) -> Path | None:
    user_sock_path = get_user_socket_path()
    sock_path = work_root / ".lspleank.sock"
    if not sock_path.exists():
        sock_path = user_sock_path
    return sock_path if sock_path.exists() else None


async def create_rpc_socket_channel(sock_path: Path) -> RpcChannel:
    loop = asyncio.get_running_loop()
    (reader, writer) = await asyncio.open_unix_connection(sock_path)
    return json_rpc_channel(reader, writer, name='socket', loop=loop)


class RpcSocketFactory(RpcDirChannelFactory):
    def __init__(self, default: RpcDirChannelFactory):
        self._default = default

    async def anew(self, work_root: Path) -> RpcChannel:
        sock_path = find_socket_path(work_root)
        if sock_path is None:
            return await self._default.anew(work_root)
        else:
            return await create_rpc_socket_channel(sock_path)


class RpcStartSocketFactory(RpcDirChannelFactory):
    def __init__(self, start_cmd: Sequence[str]):
        self._start_cmd = list(start_cmd)

    async def anew(self, work_root: Path) -> RpcChannel:
        # ignoring work_root because user socket handles any work root
        if not hasattr(asyncio, 'open_unix_connection'):
            raise NotImplementedError("This system does not support UNIX sockets")
        xdg_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
        if not xdg_dir.exists():
            raise RuntimeError("Unable to find XDG_RUNTIME_DIR")
        sock_path = xdg_dir / "lspleank.sock"
        if not sock_path.exists():
            if not self._start_cmd:
                raise RuntimeError("Missing lspleank user socket start command")
            proc = await asyncio.create_subprocess_exec(
                *self._start_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            await proc.communicate()
            if proc.returncode != 0:
                err = "Error exit code {} running {}"
                raise RuntimeError(err.format(proc.returncode, self._start_cmd))
            if not sock_path.exists():
                err = "Failed to find {} after start command {}"
                raise RuntimeError(err.format(sock_path, self._start_cmd))
        return await create_rpc_socket_channel(sock_path)


class ChannelLspInitializer(LspInitializer):
    def __init__(
        self, factory: RpcDirChannelFactory, client: RpcInterface, tg: TaskGroup
    ):
        self._factory = factory
        self._client = client
        self._tg = tg
        self._initializing: RpcInterface | None = None

    async def on_initialize(self, init_params: LspObject) -> Response:
        if self._initializing:
            return Response.from_error_code(ErrorCodes.InvalidRequest)
        lsp_root = get_uri_path(init_params, 'rootUri')
        init_call = leank_init_call(lsp_root, text_doc_caps(init_params))
        channel = await self._factory.anew(lsp_root)
        self._tg.create_task(channel.pump(self._client))
        aw_response = await channel.proxy.request(init_call)
        response = await aw_response
        if response.error is None:
            self._initializing = channel.proxy
        else:
            re = response.error
            log.error(f"LSP initialize response error {re.code}: {re.message}")
        return response

    async def do_initialized(self) -> RpcInterface | None:
        if self._initializing:
            server = self._initializing
            self._initializing = None
            await server.notify(MethodCall('initialized'))
            return server
        else:
            return None

    async def close_and_wait(self) -> None:
        if self._initializing:
            await self._initializing.close_and_wait()


def channel_lsp_server(
    factory: RpcDirChannelFactory, client: RpcInterface, tg: TaskGroup
) -> LspServer:
    return LspServer(ChannelLspInitializer(factory, client, tg))


class AsyncProgram(typing.Protocol):
    async def amain(self, stdio: DuplexStream, *, loop: AbstractEventLoop) -> int: ...
    def on_stdin_eof(self) -> None: ...


class LspProgram(AsyncProgram):
    @abc.abstractmethod
    async def start_server(self, client: RpcInterface, tg: TaskGroup) -> LspServer: ...

    async def amain(self, stdio: DuplexStream, *, loop: AbstractEventLoop) -> int:
        try:
            stdio_chan = RpcMsgChannel(JsonRpcMsgStream(stdio), name='stdio', loop=loop)
            async with TaskGroup() as tg:
                server = await self.start_server(stdio_chan.proxy, tg)
                tg.create_task(stdio_chan.pump(server))
            ok = server.was_initialized()
        except Exception as ex:
            log.exception(ex)
            return 1
        if not ok:
            log.warning("LSP server never initialized")
            return 1
        return 0

    def on_stdin_eof(self) -> None:
        pass


class AsyncMainLoopThread(threading.Thread):
    def __init__(
        self, aprog: AsyncProgram, stdio: DuplexStream, *, loop: AbstractEventLoop
    ):
        super().__init__(name=self.__class__.__name__)
        self._aprog = aprog
        self._stdio = stdio
        self._loop = loop
        self.retcode: int | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self._loop)
        coro = self._aprog.amain(self._stdio, loop=self._loop)
        self.retcode = self._loop.run_until_complete(coro)
        if not self._stdio.ain.at_eof():
            os.kill(os.getpid(), signal.SIGINT)


def async_stdio_main(aprog: AsyncProgram) -> int:
    loop = asyncio.new_event_loop()
    stdin_pump = ReadFilePump(sys.stdin.fileno(), loop=loop, on_eof=aprog.on_stdin_eof)
    writer = WriterFileAdapter(sys.stdout.buffer, loop=loop)
    stdio = DuplexStream(stdin_pump.stream, writer)
    amain_thread = AsyncMainLoopThread(aprog, stdio, loop=loop)
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
