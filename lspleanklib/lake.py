"""
Code for the Lake LSP server
"""

from asyncio import AbstractEventLoop, TaskGroup
from collections.abc import Awaitable
from pathlib import Path

from .jsonrpc import (
    ErrorCodes,
    MethodCall,
    Response,
    RpcChannel,
    RpcInterface,
    RpcSession,
    future_error,
)
from .server import (
    LspInitializer,
    LspServer,
    LspSession,
    RpcDirChannelFactory,
    RpcSessionFactory,
    leank_init_call,
    leank_init_response,
    text_doc_caps,
)
from .util import LspObject, get_uri_path, log


class LakeClient(RpcInterface):
    def __init__(self, leank_client: RpcInterface):
        self.client = leank_client

    def close(self) -> None:
        self.client.close()

    async def notify(self, mc: MethodCall) -> None:
        await self.client.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if mc.method == "client/registerCapability":
            return future_error(ErrorCodes.MethodNotFound)
        return await self.client.request(mc, fix_id)


class LeankInitializedServer(RpcInterface):
    def __init__(self, lake_server: RpcInterface):
        self._lake_server = lake_server

    def close(self) -> None:
        self._lake_server.close()

    async def notify(self, mc: MethodCall) -> None:
        await self._lake_server.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        return await self._lake_server.request(mc, fix_id)


class LeankLakeLspInitializer(LspInitializer):
    def __init__(self, channel: RpcChannel):
        self._channel = channel
        self._initializing: RpcInterface | None = None

    async def on_initialize(self, init_params: LspObject) -> Response:
        if self._initializing:
            return Response.from_error_code(ErrorCodes.InvalidRequest)
        lsp_root = get_uri_path(init_params, 'rootUri')
        init_call = leank_init_call(lsp_root, text_doc_caps(init_params))
        aw_response = await self._channel.proxy.request(init_call)
        response = await aw_response
        if response.error is None:
            self._initializing = LeankInitializedServer(self._channel.proxy)
        else:
            re = response.error
            log.error(f"LSP initialize response error {re.code}: {re.message}")
        return leank_init_response(response)

    async def do_initialized(self) -> RpcInterface | None:
        if self._initializing:
            server = self._initializing
            self._initializing = None
            await server.notify(MethodCall('initialized'))
            return server
        else:
            return None

    def close(self) -> None:
        if self._initializing:
            self._initializing.close()


class LeankLakeSession(LspSession):
    def __init__(self, channel: RpcChannel):
        self._channel = channel
        self._server = LspServer(LeankLakeLspInitializer(channel))

    def start_server(self, client: RpcInterface, tg: TaskGroup) -> RpcInterface:
        tg.create_task(self._channel.pump(LakeClient(client)))
        return self._server

    def was_initialized(self) -> bool:
        return self._server.is_initialized()


class LeankLakeSessionFactory(RpcSessionFactory):
    def __init__(self, lake_factory: RpcDirChannelFactory, loop: AbstractEventLoop):
        self._lake_factory = lake_factory
        self._loop = loop

    async def anew(self, work_dir: Path) -> RpcSession:
        chan = await self._lake_factory.anew(work_dir, self._loop)
        return LeankLakeSession(chan)
