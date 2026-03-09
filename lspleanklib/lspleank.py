"""
Link LSP-enabled editors to Lake LSP servers
"""

import argparse, asyncio, sys
from asyncio.subprocess import PIPE

from .aio import DuplexStream, ReadFilePump, WriterFileAdapter
from .cli import AsyncMainLoopThread, split_cmd_line, version
from .jsonrpc import JsonRpcDuplexConnection
from .util import log


async def msg_loop(super_io: DuplexStream, sub_io: DuplexStream) -> bool:
    super_con = JsonRpcDuplexConnection(super_io)
    sub_con = JsonRpcDuplexConnection(sub_io)
    async with asyncio.TaskGroup() as tg:
        t_super = tg.create_task(sub_con.run(super_con.remote))
        t_sub = tg.create_task(super_con.run(sub_con.remote))
    return t_super.result() and t_sub.result()


async def server_loop(super_io: DuplexStream, lsp_cmd: list[str]) -> int:
    sub_proc = await asyncio.create_subprocess_exec(*lsp_cmd, stdin=PIPE, stdout=PIPE)
    assert sub_proc.stdin and sub_proc.stdout
    sub_io = DuplexStream(sub_proc.stdout, sub_proc.stdin)
    ok = await msg_loop(super_io, sub_io)
    await sub_proc.communicate()
    return sub_proc.returncode if ok and sub_proc.returncode is not None else 1


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
