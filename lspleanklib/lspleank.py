"""
Stdio LSP server multiplexing one or more Lake LSP servers.
"""

from __future__ import annotations
import argparse, asyncio, logging, sys
from asyncio import Future, TaskGroup
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from warnings import warn

from .aio import DuplexStream
from .cli import split_cmd_line, version
from .jsonrpc import (
    ErrorCode,
    JsonRpcMsgStream,
    MethodCall,
    Response,
    RpcChannel,
    RpcInterface,
    RpcMsgConnection,
    RpcMsgFileLogger,
    awaitable_error,
)
from lspleanklib.lake import LeankLakeFactory
from .server import (
    LspInitializer,
    LspProgram,
    LspServer,
    RpcDirChannelFactory,
    RpcStartSocketFactory,
    RpcSubprocessFactory,
    async_stdio_main,
    lean_log_path,
    leank_init_call,
    leank_init_response,
    text_doc_caps,
)
from .util import (
    LspAny,
    LspObject,
    Path_from_uri,
    awaitable,
    get_obj,
    get_seq,
    get_str,
    get_uri_path,
    log,
)


LAKE_WORKSPACE_MARKER = {
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
}


class SubLeank:
    def __init__(self, work_root: Path, future_server: Future[RpcInterface]):
        self.work_root = work_root
        self._future_server = future_server
        loop = asyncio.get_running_loop()
        self._initialized_server: Future[RpcInterface | None] = loop.create_future()

    def aget_initialized_server(self) -> Future[RpcInterface | None]:
        return self._initialized_server

    async def request_initialize(self, client_capabilities: LspObject) -> Response:
        server = await self._future_server
        init_call = leank_init_call(self.work_root, client_capabilities)
        aw_response = await server.request(init_call)
        response = await aw_response
        if response.error is not None:
            self._initialized_server.set_result(None)
        return response

    async def initialized(self) -> RpcInterface | None:
        session_server = await self._future_server
        if not self._initialized_server.done():
            await session_server.notify(MethodCall('initialized'))
            self._initialized_server.set_result(session_server)
        return self._initialized_server.result()

    async def close_and_wait(self) -> None:
        if self._future_server.done():
            await self._future_server.result().close_and_wait()
        else:
            warn("LSP session closed before it could start")
            self._future_server.cancel()


class SubLeankFactory:
    def __init__(
        self, factory: RpcDirChannelFactory, client: RpcInterface, tg: TaskGroup
    ):
        self._factory = factory
        self._client = client
        self._tg = tg

    def new(self, work_dir: Path) -> SubLeank:
        aw_channel = self._factory.anew(work_dir)
        aw_server = self._pump_channel(aw_channel)
        future_server = self._tg.create_task(aw_server)
        return SubLeank(work_dir, future_server)

    async def _pump_channel(self, aw_channel: Awaitable[RpcChannel]) -> RpcInterface:
        channel = await aw_channel
        self._tg.create_task(channel.pump(self._client))
        return channel.proxy


def document_method(mc: MethodCall) -> Path | None:
    if mc.method.startswith('textDocument/'):
        try:
            return Path_from_uri(get_str(get_obj(mc.params, 'textDocument'), 'uri'))
        except ValueError as ex:
            log.exception(ex)
    return None


def pick_workspace_dir(doc_path: Path) -> Path:
    for d in doc_path.parents:
        for f in LAKE_WORKSPACE_MARKER:
            if (d / f).exists():
                return d
    log.info(f"Unable to find Lake workspace for document '{doc_path}'")
    return doc_path.parent


class LeankManager(RpcInterface):
    def __init__(self, factory: SubLeankFactory):
        self._factory = factory
        self._client_capabilities: LspObject = {}
        self._leanks: list[SubLeank] = []

    async def close_and_wait(self) -> None:
        todo = [lnk.close_and_wait() for lnk in self._leanks]
        asyncio.gather(*todo)

    async def notify(self, mc: MethodCall) -> None:
        if doc_path := document_method(mc):
            server = await self._get_server(doc_path)
            if server:
                await server.notify(mc)
        else:
            if mc.method not in {"exit"}:
                warn(f"Unexpected notification '{mc.method}'")
            async for s in self._leank_servers():
                await s.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if doc_path := document_method(mc):
            server = await self._get_server(doc_path)
            if server:
                return await server.request(mc, fix_id)
            else:
                return awaitable_error(ErrorCode.InternalError)
        elif mc.method == 'shutdown':
            async for server in self._leank_servers():
                aw_response = await server.request(MethodCall('shutdown'))
                await aw_response
            return awaitable(Response(None))
        elif mc.method == 'workspace/symbol':
            return await self._workspace_symbol(mc)
        else:
            warn(f"Unexpected request '{mc.method}'")
            return awaitable_error(ErrorCode.MethodNotFound)

    def set_client_capabilities(self, capabilities: LspObject) -> None:
        self._client_capabilities = capabilities

    async def create_sub_leank(self, work_root: Path) -> tuple[SubLeank, Response]:
        leank = self._factory.new(work_root)
        self._leanks.append(leank)
        response = await leank.request_initialize(self._client_capabilities)
        return (leank, response)

    async def _get_server(self, doc_path: Path) -> RpcInterface | None:
        lake_dir = pick_workspace_dir(doc_path)
        for leank in self._leanks:
            if leank.work_root == lake_dir:
                return await leank.aget_initialized_server()
        (leank, response) = await self.create_sub_leank(lake_dir)
        return await leank.initialized()

    async def _workspace_symbol(self, mc: MethodCall) -> Awaitable[Response]:
        result: list[LspAny] = []
        async for s in self._leank_servers():
            aw_response = await s.request(mc)
            response = await aw_response
            if response.error is not None:
                return awaitable(response)
            elif isinstance(response.result, Sequence):
                result.extend(response.result)
            else:
                return awaitable_error(ErrorCode.RequestFailed)
        return awaitable(Response(result))

    async def _leank_servers(self) -> AsyncIterator[RpcInterface]:
        for session in self._leanks:
            server = await session.aget_initialized_server()
            if server is not None:
                yield server


def workspace_folders(client_init_params: LspObject) -> Iterator[Path]:
    for folder in get_seq(client_init_params, 'workspaceFolders'):
        try:
            if isinstance(folder, Mapping):
                yield Path_from_uri(get_str(folder, 'uri'))
        except ValueError as ex:
            log.exception(ex)


class MultiLeankLspInitializer(LspInitializer):
    def __init__(
        self, factory: RpcDirChannelFactory, editor: RpcInterface, tg: TaskGroup
    ):
        self._initializing: SubLeank | None = None
        self._manager = LeankManager(SubLeankFactory(factory, editor, tg))
        self._workspace_folders: list[Path] = []

    async def on_initialize(self, init_params: LspObject) -> Response:
        if self._initializing:
            return Response.from_error_code(ErrorCode.InvalidRequest)
        # TODO warn when folders inconsistent with Lake workspaces
        self._workspace_folders.extend(workspace_folders(init_params))
        lsp_root = get_uri_path(init_params, 'rootUri')
        self._manager.set_client_capabilities(text_doc_caps(init_params))
        (self._initializing, response) = await self._manager.create_sub_leank(lsp_root)
        return leank_init_response(response)

    async def do_initialized(self) -> RpcInterface | None:
        if self._initializing:
            leank = self._initializing
            self._initializing = None
            await leank.initialized()
            return self._manager
        else:
            return None

    async def close_and_wait(self) -> None:
        if self._initializing:
            await self._initializing.close_and_wait()


def multi_leank_lsp_server(
    factory: RpcDirChannelFactory, editor: RpcInterface, tg: TaskGroup
) -> LspServer:
    return LspServer(MultiLeankLspInitializer(factory, editor, tg))


LOG_FILENAME = "lspleank.log"
STDIO_LOG_FILENAME = "lspleank-stdio.json"


class LspLeankProgram(LspProgram):
    subcmd: str
    extra_args: list[str]
    debug: bool

    def __init__(self, cmd_line_args: list[str]):
        cli = argparse.ArgumentParser(
            prog='lspleank',
            description=__doc__,
            usage=(
                "%(prog)s  [-h] [--version] [--debug] {connect,lake,stdio}"
                " [-- external_command ...]"
            ),
        )
        cli.add_argument('--version', action='version', version=version())
        cli.add_argument(
            '--debug',
            action='store_true',
            help=f"log to {LOG_FILENAME} & {STDIO_LOG_FILENAME}",
        )
        sub = cli.add_subparsers(dest='subcmd', required=True)

        sub.add_parser(
            'connect',
            help='connect to an lspleank socket service after starting it with the external command',
        )

        sub.add_parser(
            'lake',
            help="internally use lakelspout to run Leank LSP servers",
        )

        sub.add_parser(
            'stdio',
            help='use the external command to run stdio Leank LSP servers',
        )

        (cmd_line_args, self.extra_args) = split_cmd_line(cmd_line_args)
        cli.parse_args(cmd_line_args, self)
        if not self.extra_args:
            match self.subcmd:
                case 'lake':
                    self.extra_args = ['lake', 'serve']
                case 'stdio':
                    self.extra_args = ['lakelspout', 'stdio']
        if self.debug:
            log.setLevel(logging.DEBUG)  # only for lspleanklib
            fh = logging.FileHandler(lean_log_path() / LOG_FILENAME)
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter(
                fmt='%(asctime)s.%(msecs)03d|%(levelname)s|%(name)s|%(message)s',
                datefmt='%Y-%m-%dT%H:%M:%S',
            )
            fh.setFormatter(fmt)
            logging.root.addHandler(fh)

    @contextmanager
    def stdio_connection(self,  stdio: DuplexStream) -> Iterator[RpcMsgConnection]:
        stdio_conn: RpcMsgConnection = JsonRpcMsgStream(stdio)
        if self.debug:
            log_path = lean_log_path() / STDIO_LOG_FILENAME
            with open(log_path, 'a', buffering=1) as log_file:
                stdio_conn = RpcMsgFileLogger(stdio_conn, log_file)
                yield stdio_conn
        else:
            yield stdio_conn

    async def start_server(self, editor: RpcInterface, tg: TaskGroup) -> LspServer:
        loop = asyncio.get_running_loop()
        chan_factory: RpcDirChannelFactory
        if self.subcmd == 'connect':
            chan_factory = RpcStartSocketFactory(self.extra_args)
        else:
            chan_factory = RpcSubprocessFactory(self.extra_args, loop=loop)
        if self.subcmd == 'lake':
            chan_factory = LeankLakeFactory(chan_factory)
        return multi_leank_lsp_server(chan_factory, editor, tg)


def lspleank_connect_main(start_cmd: Sequence[str]) -> int:
    cmd_line_args = ['connect', '--', *start_cmd]
    return async_stdio_main(LspLeankProgram(cmd_line_args))


def main(cmd_line_args: list[str] | None = None) -> int:
    logging.basicConfig()
    logging.captureWarnings(True)
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    return async_stdio_main(LspLeankProgram(cmd_line_args))


if __name__ == '__main__':
    exit(main())
