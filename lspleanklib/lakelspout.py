"""
Adapt Lake LSP server to be a Leank LSP server
"""

import asyncio, argparse, logging, os, sys
from asyncio import AbstractEventLoop
from contextlib import suppress
from pathlib import Path

from .aio import DuplexStream
from .cli import split_cmd_line, version
from .jsonrpc import JsonRpcChannel, RpcChannel
from .lake import LeankLakeSession
from .server import (
    AsyncProgram,
    LspProgram,
    LspSession,
    RpcSubprocess,
    async_stdio_main,
    lsp_server_loop,
)


class StdioProgram(LspProgram):
    def __init__(self, lake_cmd: list[str]):
        self._lake_cmd = lake_cmd

    async def aget_session(self, loop: AbstractEventLoop) -> LspSession:
        lake_chan = await RpcSubprocess.anew(self._lake_cmd, Path.cwd(), loop)
        return LeankLakeSession(lake_chan)


class LeankLakeConnection:
    def __init__(self, lake_cmd: list[str], tg: asyncio.TaskGroup):
        self._lake_cmd = lake_cmd
        self._tg = tg
        self._loop = asyncio.get_running_loop()

    def on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        print("Socket connected")
        aio = DuplexStream(reader, writer)
        sock_chan = JsonRpcChannel(aio, self._loop, 'socket')
        self._tg.create_task(self._async_on_connect(sock_chan))

    async def _async_on_connect(self, sock_chan: RpcChannel) -> None:
        lake_chan = await RpcSubprocess.anew(self._lake_cmd, Path.cwd(), self._loop)
        try:
            await lsp_server_loop(LeankLakeSession(lake_chan), sock_chan)
        except Exception as ex:
            print(ex, file=sys.stderr)
        print("LSP server done")


class WorkProgram(AsyncProgram):
    def __init__(self, lake_cmd: list[str]):
        self._lake_cmd = lake_cmd
        self._stdin_eof = asyncio.Event()

    async def amain(self, stdio: DuplexStream, loop: AbstractEventLoop) -> int:
        sock_path = Path.cwd() / ".lspleank.sock"
        if sock_path.exists():
            errmsg = (
                f"workspace directory socket already in use: {sock_path}"
                "; delete if not being used"
            )
            print(errmsg, file=sys.stderr)
            return 1
        print("Press CTRL-D (EOF) to stop waiting for new connections ...", flush=True)
        try:
            async with asyncio.TaskGroup() as tg:
                session = LeankLakeConnection(self._lake_cmd, tg)
                socket_server = await asyncio.start_unix_server(
                    session.on_connect, sock_path, backlog=1
                )
                async with socket_server:
                    print(f"Listening on {sock_path}...", flush=True)
                    await self._stdin_eof.wait()
                    socket_server.close()
                    print("Waiting for connections to finish...", flush=True)
                    await socket_server.wait_closed()
        except Exception as ex:
            print(ex, file=sys.stderr)
            return 1
        finally:
            with suppress(FileNotFoundError):
                os.unlink(sock_path)
        return 0

    def on_stdin_eof(self) -> None:
        self._stdin_eof.set()


def main(cmd_line_args: list[str] | None = None) -> int:
    logging.basicConfig()
    logging.captureWarnings(True)

    cli = argparse.ArgumentParser(prog='lakelspout', description=__doc__)
    cli.add_argument('--version', action='version', version=version())
    cli.add_argument('command', choices=['work', 'stdio'])

    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    (cli_args, extra_args) = split_cmd_line(cmd_line_args, ['lake', 'serve'])
    args = cli.parse_args(cli_args)

    aprog: AsyncProgram
    match args.command:
        case 'work':
            aprog = WorkProgram(extra_args)
        case 'stdio':
            aprog = StdioProgram(extra_args)
    return async_stdio_main(aprog)


if __name__ == '__main__':
    exit(main())
