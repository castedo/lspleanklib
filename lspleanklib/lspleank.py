"""
Link LSP-enabled editors to Lake LSP servers
"""

from __future__ import annotations
import argparse, logging, os, sys
from asyncio import Future, TaskGroup
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from pathlib import Path
from warnings import warn

from .cli import split_cmd_line, version
from .jsonrpc import (
    ErrorCodes,
    MethodCall,
    Response,
    RpcDuplexChannel,
    RpcInterface,
    future_error,
)
from .server import (
    LspServer,
    LspService,
    RpcSubprocessFactory,
    RpcDirChannelFactory,
    async_stdio_main,
)
from .util import (
    LspAny,
    LspObject,
    Path_from_uri,
    awaitable,
    get_obj,
    get_seq,
    get_str,
    log,
)


LSP_SERVER_NAME = "lspleank"
LSP_CLIENT_NAME = LSP_SERVER_NAME


LAKE_WORKSPACE_MARKER = {
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
    ".lspleank.sock",
}


class LeankMultiClient(RpcInterface):
    def __init__(self, editor: RpcInterface, editor_caps: LspObject):
        self._editor = editor
        self._caps = {'textDocument': editor_caps.get('textDocument', {})}

    def close(self) -> None:
        self._editor.close()

    async def notify(self, mc: MethodCall) -> None:
        await self._editor.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        return await self._editor.request(mc, fix_id)

    def init_call(self, work_root: Path) -> MethodCall:
        return MethodCall(
            'initialize',
            {
                'capabilities': self._caps,
                'clientInfo': {'name': LSP_CLIENT_NAME, 'version': version()},
                'processId': os.getpid(),
                'rootUri': work_root.as_uri(),
            },
        )


class LeankSession:
    def __init__(
        self,
        client: LeankMultiClient,
        work_root: Path,
        future_channel: Future[RpcDuplexChannel],
        tg: TaskGroup,
    ):
        self.client = client
        self.work_root = work_root
        self._future_channel = future_channel
        self._initialize_task = tg.create_task(self._initialize())
        self._initialized_server: RpcDuplexChannel | None = None

    async def pump(self) -> None:
        channel = await self._future_channel
        await channel.pump(self.client)

    async def _initialize(self) -> Response:
        server = await self._future_channel
        init_call = self.client.init_call(self.work_root)
        aw_response = await server.proxy.request(init_call)
        response = await aw_response
        if response.error is not None:
            log.error(f"Server initialization failed for workspace '{self.work_root}'")
        else:
            log.debug(f"Server initialize response for workspace '{self.work_root}'")
        return response

    def close(self) -> None:
        log.debug(f"closing {self.__class__.__name__}")
        if self._initialized_server:
            self._initialized_server.proxy.close()
        elif self._future_channel.done():
            self._future_channel.result().proxy.close()
        else:
            self._future_channel.cancel("Leank session closed before server run")

    async def initialize_response(self) -> Response:
        return await self._initialize_task

    async def initialized(self) -> None:
        self._initialized_server = await self._future_channel
        await self._initialized_server.proxy.notify(MethodCall('initialized'))
        log.debug(f"Server initialized for workspace '{self.work_root}'")

    async def notify(self, mc: MethodCall) -> None:
        if self._initialized_server:
            await self._initialized_server.proxy.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if self._initialized_server:
            return await self._initialized_server.proxy.request(mc, fix_id)
        else:
            return future_error(ErrorCodes.ServerNotInitialized)


class LeankSessionFactory:
    def __init__(self, factory: RpcDirChannelFactory, tg: TaskGroup):
        self._factory = factory
        self._tg = tg

    def new(self, client: LeankMultiClient, work_root: Path) -> LeankSession:
        future_channel = self._tg.create_task(self._factory.anew(work_root))
        sess = LeankSession(client, work_root, future_channel, self._tg)
        self._tg.create_task(sess.pump())
        return sess


def adapt_init_response(lakish_server_init_result: Response) -> Response:
    if lakish_server_init_result.error is not None:
        return lakish_server_init_result
    else:
        if isinstance(lakish_server_init_result.result, dict):
            # TODO check and standardize server caps
            server_caps = lakish_server_init_result.result.get('capabilities')
            server_caps = server_caps if isinstance(server_caps, dict) else {}
        else:
            server_caps = {}
    response = Response(
        {
            'capabilities': server_caps,
            'serverInfo': {'name': LSP_SERVER_NAME, 'version': version()},
        }
    )
    return response


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


class LeankManager:
    def __init__(self, init_session: LeankSession, factory: LeankSessionFactory):
        self._client = init_session.client
        self._sessions = [init_session]
        self._factory = factory

    def close(self) -> None:
        for s in self._sessions:
            s.close()

    async def notify(self, mc: MethodCall) -> None:
        if doc_path := document_method(mc):
            sess = await self._get_session(doc_path)
            await sess.notify(mc)
        else:
            if mc.method not in {"exit"}:
                warn(f"Unexpected notification '{mc.method}'")
            for s in self._sessions:
                await s.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if doc_path := document_method(mc):
            sess = await self._get_session(doc_path)
            return await sess.request(mc, fix_id)
        elif mc.method == 'shutdown':
            for s in self._sessions:
                aw_response = await s.request(MethodCall('shutdown'))
                await aw_response
            return awaitable(Response(None))
        elif mc.method == 'workspace/symbol':
            return await self._workspace_symbol(mc)
        else:
            warn(f"Unexpected request '{mc.method}'")
            return future_error(ErrorCodes.MethodNotFound)

    async def _get_session(self, doc_path: Path) -> RpcInterface:
        lake_dir = pick_workspace_dir(doc_path)
        for s in self._sessions:
            if s.work_root == lake_dir:
                return s
        sess = self._factory.new(self._client, lake_dir)
        self._sessions.append(sess)
        await sess.initialize_response()
        await sess.initialized()
        return sess

    async def _workspace_symbol(self, mc: MethodCall) -> Awaitable[Response]:
        result: list[LspAny] = []
        for s in self._sessions:
            aw_response = await s.request(mc)
            response = await aw_response
            if response.error is not None:
                return awaitable(response)
            elif isinstance(response.result, Sequence):
                result.extend(response.result)
            else:
                return future_error(ErrorCodes.RequestFailed)
        return awaitable(Response(result))


def workspace_folders(client_init_params: LspObject) -> Iterator[Path]:
    for folder in get_seq(client_init_params, 'workspaceFolders'):
        try:
            if isinstance(folder, Mapping):
                yield Path_from_uri(get_str(folder, 'uri'))
        except ValueError as ex:
            log.exception(ex)


class MultiLeankLspServer(LspServer):
    def __init__(
        self, editor: RpcInterface, factory: RpcDirChannelFactory, tg: TaskGroup
    ):
        self._editor = editor
        self._factory = LeankSessionFactory(factory, tg)
        self._workspace_folders: list[Path] = []
        self._initializing: LeankSession | None = None
        self._initialized: LeankManager | None = None

    def is_initialized(self) -> bool:
        return self._initialized is not None

    def close(self) -> None:
        log.debug(f"closing {self.__class__.__name__}")
        if self._initializing:
            self._initializing.close()
        if self._initialized:
            self._initialized.close()

    async def notify(self, mc: MethodCall) -> None:
        if self._initialized:
            await self._initialized.notify(mc)
        elif self._initializing and mc.method == 'initialized':
            first_sess = self._initializing
            self._initializing = None
            self._initialized = LeankManager(first_sess, self._factory)
            await first_sess.initialized()
        else:
            what = "initializing" if self._initializing else "not initializing"
            warn(f"Got '{mc.method}' notification when {what}")

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if mc.method == 'initialize':
            init_params = mc.params if isinstance(mc.params, dict) else {}
            response = await self._initalize(init_params)
            return awaitable(response)
        elif self._initialized:
            return await self._initialized.request(mc, fix_id)
        else:
            return future_error(ErrorCodes.ServerNotInitialized)

    async def _initalize(self, init_params: LspObject) -> Response:
        if self._initializing or self._initialized:
            return Response.from_error_code(ErrorCodes.InvalidRequest)
        # TODO warn when folders inconsistent with Lake workspaces
        self._workspace_folders.extend(workspace_folders(init_params))
        client = LeankMultiClient(self._editor, get_obj(init_params, 'capabilities'))
        root_uri = get_str(init_params, 'rootUri')
        work_root = Path.cwd() if not root_uri else Path_from_uri(root_uri)
        first_sess = self._factory.new(client, work_root)
        response = await first_sess.initialize_response()
        if response.error is None:
            self._initializing = first_sess
        return adapt_init_response(response)


class MultiLeankLspService(LspService):
    def __init__(self, factory: RpcDirChannelFactory):
        self._factory = factory

    def start(self, client: RpcInterface, tg: TaskGroup) -> LspServer:
        return MultiLeankLspServer(client, self._factory, tg)


def main(cmd_line_args: list[str] | None = None) -> int:
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    (cmd_line_args, extra_args) = split_cmd_line(cmd_line_args)

    logging.basicConfig()
    logging.captureWarnings(True)

    cli = argparse.ArgumentParser(prog='lspleank', description=__doc__)
    cli.add_argument('--version', action='version', version=version())
    cli.add_argument('command', choices=['stdio'])
    cli.parse_args(cmd_line_args)

    factory: RpcDirChannelFactory = RpcSubprocessFactory(extra_args)
    service = MultiLeankLspService(factory)
    return async_stdio_main(service.amain)


if __name__ == '__main__':
    exit(main())
