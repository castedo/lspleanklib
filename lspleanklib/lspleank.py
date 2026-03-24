"""
Link LSP-enabled editors to Lake LSP servers
"""

from __future__ import annotations
import argparse, logging, sys
from asyncio import AbstractEventLoop, Future, TaskGroup
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from warnings import warn

from .cli import split_cmd_line, version
from .jsonrpc import (
    ErrorCodes,
    MethodCall,
    Response,
    RpcInterface,
    RpcSession,
    future_error,
)
from lspleanklib.lake import LeankLakeSessionFactory
from .server import (
    ChannelRpcSessionFactory,
    LspInitializer,
    LspProgram,
    LspServer,
    LspSession,
    RpcSessionFactory,
    RpcSubprocessFactory,
    async_stdio_main,
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
    ".lspleank.sock",
}


async def aget_session_server(
    aw_session: Awaitable[RpcSession], client: RpcInterface, tg: TaskGroup
) -> RpcInterface:
    session = await aw_session
    return session.start_server(client, tg)


@dataclass
class SubLeank:
    work_root: Path
    future_server: Future[RpcInterface]

    def close(self) -> None:
        if self.future_server.done():
            self.future_server.result().close()
        else:
            warn("LSP session closed before it could start")
            self.future_server.cancel()


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
    def __init__(
        self,
        editor: RpcInterface,
        factory: RpcSessionFactory,
        tg: TaskGroup,
    ):
        self._editor = editor
        self._leanks: list[SubLeank] = []
        self._factory = factory
        self._tg = tg
        self._client_capabilities: LspObject = {}

    def add_leank(self, leank: SubLeank) -> None:
        self._leanks.append(leank)

    def close(self) -> None:
        for leank in self._leanks:
            leank.close()

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
                return future_error(ErrorCodes.InternalError)
        elif mc.method == 'shutdown':
            async for server in self._leank_servers():
                aw_response = await server.request(MethodCall('shutdown'))
                await aw_response
            return awaitable(Response(None))
        elif mc.method == 'workspace/symbol':
            return await self._workspace_symbol(mc)
        else:
            warn(f"Unexpected request '{mc.method}'")
            return future_error(ErrorCodes.MethodNotFound)

    def create_sub_leank(self, work_root: Path) -> SubLeank:
        aw_session = self._factory.anew(work_root)
        aw_server = aget_session_server(aw_session, self._editor, self._tg)
        future_server = self._tg.create_task(aw_server)
        return SubLeank(work_root, future_server)

    def set_client_capabilities(self, capabilities: LspObject) -> None:
        self._client_capabilities = capabilities

    async def request_initialize(self, work_root: Path, server: RpcInterface) -> Response:
        init_call = leank_init_call(work_root, self._client_capabilities)
        aw_response = await server.request(init_call)
        return await aw_response

    async def _get_server(self, doc_path: Path) -> RpcInterface | None:
        lake_dir = pick_workspace_dir(doc_path)
        for leank in self._leanks:
            if leank.work_root == lake_dir:
                return await leank.future_server

        leank = self.create_sub_leank(lake_dir)
        self._leanks.append(leank)

        server = await leank.future_server
        # TODO a non initialize method call might be queue to send to server now
        response = await self.request_initialize(lake_dir, server)
        if response.error is None:
            await server.notify(MethodCall('initialized'))
            return server
        else:
            re = response.error
            warn(f"LSP server initialize response error {re.code}: {re.message}")
            return None

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
                return future_error(ErrorCodes.RequestFailed)
        return awaitable(Response(result))

    async def _leank_servers(self) -> AsyncIterator[RpcInterface]:
        for session in self._leanks:
            yield await session.future_server


def workspace_folders(client_init_params: LspObject) -> Iterator[Path]:
    for folder in get_seq(client_init_params, 'workspaceFolders'):
        try:
            if isinstance(folder, Mapping):
                yield Path_from_uri(get_str(folder, 'uri'))
        except ValueError as ex:
            log.exception(ex)


class MultiLeankLspInitializer(LspInitializer):
    def __init__(self, factory: RpcSessionFactory, editor: RpcInterface, tg: TaskGroup):
        self._initializing: SubLeank | None = None
        self._manager = LeankManager(editor, factory, tg)
        self._workspace_folders: list[Path] = []

    async def on_initialize(self, init_params: LspObject) -> Response:
        if self._initializing:
            return Response.from_error_code(ErrorCodes.InvalidRequest)

        # TODO warn when folders inconsistent with Lake workspaces
        self._workspace_folders.extend(workspace_folders(init_params))

        lsp_root = get_uri_path(init_params, 'rootUri')
        self._manager.set_client_capabilities(text_doc_caps(init_params))
        leank = self._manager.create_sub_leank(lsp_root)

        server = await leank.future_server
        response = await self._manager.request_initialize(lsp_root, server)
        if response.error is None:
            self._initializing = leank
        return leank_init_response(response.result)

    async def do_initialized(self) -> RpcInterface | None:
        ret = None
        if self._initializing:
            leank = self._initializing
            server = await leank.future_server
            self._initializing = None
            ret = self._manager
            ret.add_leank(leank)
            await server.notify(MethodCall('initialized'))
        return ret

    def close(self) -> None:
        if self._initializing:
            self._initializing.close()


class MultiLeankLspSession(LspSession):
    def __init__(self, factory: RpcSessionFactory):
        self._factory = factory
        self._started: LspServer | None = None

    def start_server(self, editor: RpcInterface, tg: TaskGroup) -> RpcInterface:
        initializer = MultiLeankLspInitializer(self._factory, editor, tg)
        self._started = LspServer(initializer)
        return self._started

    def was_initialized(self) -> bool:
        return self._started is not None and self._started.is_initialized()


class LspLeankProgram(LspProgram):
    command: str
    extra_args: list[str]

    def __init__(self, cmd_line_args: list[str]):
        cli = argparse.ArgumentParser(prog='lspleank', description=__doc__)
        cli.add_argument('--version', action='version', version=version())
        cli.add_argument('command', choices=['lake', 'stdio'])

        (cmd_line_args, self.extra_args) = split_cmd_line(cmd_line_args)
        cli.parse_args(cmd_line_args, self)
        if not self.extra_args:
            match self.command:
                case 'lake':
                    self.extra_args = ['lake', 'serve']
                case 'stdio':
                    self.extra_args = ['lakelspout', 'stdio']

    async def aget_session(self, loop: AbstractEventLoop) -> LspSession:
        chan_factory = RpcSubprocessFactory(self.extra_args)
        sess_factory: RpcSessionFactory
        if self.command == 'lake':
            sess_factory = LeankLakeSessionFactory(chan_factory, loop)
        else:
            sess_factory = ChannelRpcSessionFactory(chan_factory, loop)
        return MultiLeankLspSession(sess_factory)


def main(cmd_line_args: list[str] | None = None) -> int:
    logging.basicConfig()
    logging.captureWarnings(True)
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    return async_stdio_main(LspLeankProgram(cmd_line_args))


if __name__ == '__main__':
    exit(main())
