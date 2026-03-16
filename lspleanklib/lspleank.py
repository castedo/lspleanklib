"""
Link LSP-enabled editors to Lake LSP servers
"""

from __future__ import annotations
import argparse, asyncio, logging, sys, typing
from asyncio import TaskGroup, subprocess
from collections.abc import Awaitable
from pathlib import Path

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .cli import AsyncMainLoopThread, split_cmd_line, version
from .jsonrpc import (
    ErrorCodes,
    JsonRpcDuplexConnection,
    MethodCall,
    Response,
    RpcInterface,
    future_error,
)
from .util import log


class LeankSession(typing.Protocol):
    @property
    def proxy(self) -> RpcInterface: ...

    async def run(self, editor: RpcInterface) -> bool: ...

    def done_ok(self) -> bool: ...


class LeankFactory(typing.Protocol):
    async def anew(self, root_dir: Path) -> LeankSession: ...


class SubprocessLeankFactory(LeankFactory):
    def __init__(self, lsp_cmd: list[str]):
        self._lsp_cmd = lsp_cmd

    async def anew(self, root_dir: Path) -> LeankSession:
        proc = await asyncio.create_subprocess_exec(
            *self._lsp_cmd, cwd=root_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        return SubprocessLeankSession(proc)


class SubprocessLeankSession(LeankSession):
    def __init__(self, proc: subprocess.Process):
        assert proc.stdin and proc.stdout
        self._proc = proc
        aio = DuplexStream(proc.stdout, proc.stdin)
        self._sub_con = JsonRpcDuplexConnection(aio, 'leank')

    @property
    def proxy(self) -> RpcInterface:
        return self._sub_con.proxy

    def done_ok(self) -> bool:
        return self._proc.returncode == 0

    async def run(self, editor: RpcInterface) -> bool:
        await self._sub_con.pump(editor)
        await self._proc.communicate()
        return self.done_ok()


class StandardizedLspServer(RpcInterface):
    def __init__(self, factory: LeankFactory, editor: RpcInterface, tg: TaskGroup):
        self._factory = factory
        self._editor = editor
        self._tg = tg
        self._session: LeankSession | None = None

    def sessions_done_ok(self) -> bool:
        return self._session is not None and self._session.done_ok()

    async def notify(self, mc: MethodCall) -> None:
        if self._session is not None:
            await self._session.proxy.notify(mc)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if self._session is None and mc.method == 'initialize':
            self._session = await self._factory.anew(Path.cwd())
            self._tg.create_task(self._session.run(self._editor))
        if self._session is not None:
            return await self._session.proxy.request(mc, fix_id)
        else:
            return future_error(ErrorCodes.ServerNotInitialized)


async def server_loop(stdio: DuplexStream, factory: LeankFactory) -> int:
    editor_con = JsonRpcDuplexConnection(stdio, 'stdio')
    async with TaskGroup() as tg:
        server = StandardizedLspServer(factory, editor_con.proxy, tg)
        t_serv = tg.create_task(editor_con.pump(server))
    ok = t_serv.result() and server.sessions_done_ok()
    return 0 if ok else 1


def get_extern_cmd(cmd_line_args: list[str]) -> list[str]:
    cli = argparse.ArgumentParser(prog='lspleank', description=__doc__)
    cli.add_argument('--version', action='version', version=version())
    cli.add_argument('command', choices=['stdio'])
    (args, extra) = split_cmd_line(cmd_line_args, ['lake', 'serve'])
    cli.parse_args(args)
    return extra


def main(cmd_line_args: list[str] | None = None) -> int:
    logging.basicConfig()
    logging.captureWarnings(True)
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    extern_cmd = get_extern_cmd(cmd_line_args)
    loop = asyncio.new_event_loop()
    pump = ReadFilePump(sys.stdin.fileno(), loop)
    stdio = DuplexStream(pump.stream, WriterFileAdapter(sys.stdout.buffer, loop))
    factory = SubprocessLeankFactory(extern_cmd)
    amain_thread = AsyncMainLoopThread(loop, server_loop(stdio, factory))
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
