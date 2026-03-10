"""
Link LSP-enabled editors to Lake LSP servers
"""

from __future__ import annotations
import argparse, asyncio, sys
from asyncio.subprocess import PIPE, Process
from collections.abc import Awaitable

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .cli import AsyncMainLoopThread, split_cmd_line, version
from .jsonrpc import (
    ErrorCodes,
    JsonRpcDuplexConnection,
    MethodCall,
    Response,
    RpcInterface,
)
from .util import log


class LeankSession:
    def __init__(self, proc: Process):
        assert proc.stdin and proc.stdout
        self._proc = proc
        self.sub_con = JsonRpcDuplexConnection(DuplexStream(proc.stdout, proc.stdin))

    @staticmethod
    async def anew(lsp_cmd: list[str]) -> LeankSession:
        proc = await asyncio.create_subprocess_exec(*lsp_cmd, stdin=PIPE, stdout=PIPE)
        return LeankSession(proc)

    @property
    def remote_interface(self) -> RpcInterface:
        return self.sub_con.remote

    async def run(self, editor: RpcInterface) -> bool:
        await self.sub_con.run(editor)
        await self._proc.communicate()
        return self._proc.returncode == 0


async def future_error(ec: ErrorCodes) -> Response:
    return Response.from_error_code(ec)


class StandardizedLspServer(RpcInterface):
    def __init__(self, lsp_cmd: list[str]):
        self._lsp_cmd = lsp_cmd
        self._session: LeankSession | None = None

    async def run(self, super_con: JsonRpcDuplexConnection) -> bool:
        async with asyncio.TaskGroup() as tg:
            self._session = await LeankSession.anew(self._lsp_cmd)
            t_serv = tg.create_task(super_con.run(self))
            t_sess = tg.create_task(self._session.run(super_con.remote))
        return t_serv.result() and t_sess.result()

    async def notify(self, mc: MethodCall) -> None:
        if self._session is not None:
            await self._session.remote_interface.notify(mc)

    async def request(self, mc: MethodCall, fix_id: str | None) -> Awaitable[Response]:
        if self._session is not None:
            return await self._session.remote_interface.request(mc, fix_id)
        else:
            return future_error(ErrorCodes.ServerNotInitialized)

    def release(self) -> None:
        if self._session is not None:
            self._session.remote_interface.release()


async def server_loop(stdio: DuplexStream, lsp_cmd: list[str]) -> int:
    super_con = JsonRpcDuplexConnection(stdio)
    lsp_server = StandardizedLspServer(lsp_cmd)
    return 0 if await lsp_server.run(super_con) else 1


def get_extern_cmd(cmd_line_args: list[str]) -> list[str]:
    cli = argparse.ArgumentParser(prog='lspleank', description=__doc__)
    cli.add_argument('--version', action='version', version=version())
    cli.add_argument('command', choices=['stdio'])
    (args, extra) = split_cmd_line(cmd_line_args, ['lake', 'serve'])
    cli.parse_args(args)
    return extra


def main(cmd_line_args: list[str] | None = None) -> int:
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    extern_cmd = get_extern_cmd(cmd_line_args)
    loop = asyncio.new_event_loop()
    pump = ReadFilePump(sys.stdin.fileno(), loop)
    stdio = DuplexStream(pump.stream, WriterFileAdapter(sys.stdout.buffer, loop))
    amain_thread = AsyncMainLoopThread(loop, server_loop(stdio, extern_cmd))
    amain_thread.start()
    pump.run()
    try:
        amain_thread.join()
    except KeyboardInterrupt as ex:
        log(ex)
    if amain_thread.retcode is None:
        log('Async main loop thread did not complete properly')
        return 1
    return amain_thread.retcode
