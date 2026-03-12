"""
Link LSP-enabled editors to Lake LSP servers
"""

from __future__ import annotations
import argparse, asyncio, logging, sys
from asyncio import TaskGroup
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
    future_error,
)
from .util import log


class LeankSession:
    def __init__(self, proc: Process):
        assert proc.stdin and proc.stdout
        self._proc = proc
        self._sub_con = JsonRpcDuplexConnection(DuplexStream(proc.stdout, proc.stdin))

    def close(self) -> None:
        self.proxy.close()

    @staticmethod
    async def anew(lsp_cmd: list[str]) -> LeankSession:
        proc = await asyncio.create_subprocess_exec(*lsp_cmd, stdin=PIPE, stdout=PIPE)
        return LeankSession(proc)

    @property
    def proxy(self) -> RpcInterface:
        return self._sub_con.remote

    def done_ok(self) -> bool:
        return self._proc.returncode == 0

    async def run(self, editor: RpcInterface) -> bool:
        await self._sub_con.run(editor)
        await self._proc.communicate()
        return self.done_ok()


class StandardizedLspServer(RpcInterface):
    def __init__(self, lsp_cmd: list[str], editor: RpcInterface, tg: TaskGroup):
        self._lsp_cmd = lsp_cmd
        self._editor = editor
        self._tg = tg
        self._session: LeankSession | None = None

    def sessions_done_ok(self) -> bool:
        return self._session is None or self._session.done_ok()

    async def notify(self, mc: MethodCall) -> None:
        if self._session is not None:
            await self._session.proxy.notify(mc)

    async def request(self, mc: MethodCall, fix_id: str | None) -> Awaitable[Response]:
        if self._session is None and mc.method == 'initialize':
            self._session = await LeankSession.anew(self._lsp_cmd)
            self._tg.create_task(self._session.run(self._editor))
        if self._session is not None:
            return await self._session.proxy.request(mc, fix_id)
        else:
            return future_error(ErrorCodes.ServerNotInitialized)

    def close(self) -> None:
        if self._session is None:
            self._editor.close()
        else:
            self._session.close()


async def server_loop(stdio: DuplexStream, lsp_cmd: list[str]) -> int:
    editor_con = JsonRpcDuplexConnection(stdio)
    async with TaskGroup() as tg:
        server = StandardizedLspServer(lsp_cmd, editor_con.remote, tg)
        t_serv = tg.create_task(editor_con.run(server))
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
    amain_thread = AsyncMainLoopThread(loop, server_loop(stdio, extern_cmd))
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
