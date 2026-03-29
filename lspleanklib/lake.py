"""
Code for the Lake LSP server
"""

from collections.abc import Awaitable
from pathlib import Path

from .jsonrpc import (
    ErrorCode,
    MethodCall,
    Response,
    RpcChannel,
    RpcInterface,
    awaitable_error,
)
from .server import (
    RpcDirChannelFactory,
    leank_init_call,
    leank_init_response,
    text_doc_caps,
)
from .util import awaitable, get_uri_path


class LakeClient(RpcInterface):
    def __init__(self, leank_client: RpcInterface):
        self.client = leank_client

    async def close_and_wait(self) -> None:
        await self.client.close_and_wait()

    async def notify(self, mc: MethodCall) -> None:
        await self.client.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if mc.method == "client/registerCapability":
            return awaitable_error(ErrorCode.MethodNotFound)
        return await self.client.request(mc, fix_id)


class LeankServer(RpcInterface):
    def __init__(self, lake_server: RpcInterface):
        self._lake_server = lake_server

    async def close_and_wait(self) -> None:
        await self._lake_server.close_and_wait()

    async def notify(self, mc: MethodCall) -> None:
        await self._lake_server.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if mc.method != "initialized":
            return await self._lake_server.request(mc)
        else:
            init_params = mc.params if isinstance(mc.params, dict) else {}
            lsp_root = get_uri_path(init_params, 'rootUri')
            init_call = leank_init_call(lsp_root, text_doc_caps(init_params))
            aw_response = await self._lake_server.request(init_call)
            response = await aw_response
            return awaitable(leank_init_response(response))


class LeankLakeChannel(RpcChannel):
    def __init__(self, lake_channel: RpcChannel):
        self._lake_channel = lake_channel
        self._leank_server = LeankServer(lake_channel.proxy)

    @property
    def proxy(self) -> RpcInterface:
        return self._leank_server

    async def pump(self, leank_client: RpcInterface | None = None) -> None:
        impl = None if leank_client is None else LakeClient(leank_client)
        await self._lake_channel.pump(impl)


class LeankLakeFactory(RpcDirChannelFactory):
    def __init__(self, lake_factory: RpcDirChannelFactory):
        self._lake_factory = lake_factory

    async def anew(self, work_root: Path) -> RpcChannel:
        lake_channel = await self._lake_factory.anew(work_root)
        return LeankLakeChannel(lake_channel)
