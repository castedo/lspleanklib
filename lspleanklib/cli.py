"""
Link LSP-enabled editors to Lake LSP servers
"""

import argparse, asyncio, sys, threading
from asyncio import AbstractEventLoop
from asyncio.subprocess import PIPE
from collections.abc import Awaitable


from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .jsonrpc import JsonRpcDuplexConnection, relay_rpc_loop
from .util import log


async def msg_loop(super_io: DuplexStream, sub_io: DuplexStream) -> bool:
    super_con = JsonRpcDuplexConnection(super_io)
    sub_con = JsonRpcDuplexConnection(sub_io)
    async with asyncio.TaskGroup() as tg:
        t_super = tg.create_task(relay_rpc_loop(super_con.remote, sub_con))
        t_sub = tg.create_task(relay_rpc_loop(sub_con.remote, super_con))
    return t_super.result() and t_sub.result()


async def server_loop(super_io: DuplexStream, lsp_cmd: tuple[str, ...]) -> int:
    sub_proc = await asyncio.create_subprocess_exec(*lsp_cmd, stdin=PIPE, stdout=PIPE)
    assert sub_proc.stdin and sub_proc.stdout
    sub_io = DuplexStream(sub_proc.stdout, sub_proc.stdin)
    ok = await msg_loop(super_io, sub_io)
    await sub_proc.communicate()
    return sub_proc.returncode if ok and sub_proc.returncode is not None else 1


class AsyncMainLoopThread(threading.Thread):
    def __init__(self, loop: AbstractEventLoop, amain: Awaitable[int]):
        super().__init__(name=self.__class__.__name__)
        self._loop = loop
        self._amain = amain
        self.retcode: int | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self.retcode = self._loop.run_until_complete(self._amain)


def version() -> str:
    try:
        from ._version import version  # type: ignore[import-not-found]

        return str(version)
    except ImportError:
        return '0.0.0'


def get_extern_cmd(cmd_line_args: list[str]) -> tuple[str, ...]:
    cli = argparse.ArgumentParser(prog="lspleank", description=__doc__)
    cli.add_argument('--version', action='version', version=version())
    cli.add_argument('command', choices=['stdio'])
    try:
        cut = cmd_line_args.index('--')
    except ValueError:
        cut = len(cmd_line_args)
    cli.parse_args(cmd_line_args[:cut])
    extern_cmd = tuple(cmd_line_args[cut + 1 :])
    return extern_cmd or ('lake', 'serve')


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
