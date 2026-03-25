"""
Adapt Lake LSP server to be a Leank LSP server
"""

import asyncio, argparse, logging, os, sys
from asyncio import AbstractEventLoop
from contextlib import suppress
from pathlib import Path

from .aio import DuplexStream
from .cli import split_cmd_line, version
from .jsonrpc import JsonRpcChannel
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
        self.lake_cmd = lake_cmd

    async def aget_session(self, loop: AbstractEventLoop) -> LspSession:
        lake_chan = await RpcSubprocess.anew(self.lake_cmd, Path.cwd(), loop)
        return LeankLakeSession(lake_chan)


class WorkProgram(AsyncProgram):
    def __init__(self, lake_cmd: list[str]):
        self.lake_cmd = lake_cmd
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
            socket_server = await asyncio.start_unix_server(
                self._on_connect, sock_path, backlog=1
            )
            async with socket_server:
                print(f"Listening on {sock_path}...", flush=True)
                await self._stdin_eof.wait()
        except Exception as ex:
            print(ex, file=sys.stderr)
            return 1
        finally:
            with suppress(FileNotFoundError):
                os.unlink(sock_path)
        return 0

    def on_stdin_eof(self) -> None:
        self._stdin_eof.set()

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        print("Socket connected")
        aio = DuplexStream(reader, writer)
        loop = asyncio.get_running_loop()
        sock_chan = JsonRpcChannel(aio, loop, 'socket')
        lake_chan = await RpcSubprocess.anew(self.lake_cmd, Path.cwd(), loop)
        session = LeankLakeSession(lake_chan)
        try:
            await lsp_server_loop(session, sock_chan)
        except Exception as ex:
            print(ex, file=sys.stderr)
        print("LSP server loop done")


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
        case 'stdio':
            aprog = StdioProgram(extra_args)
        case 'work':
            aprog = WorkProgram(extra_args)
    return async_stdio_main(aprog)


if __name__ == '__main__':
    exit(main())
