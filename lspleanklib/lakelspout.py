"""
Adapt Lake LSP server to be a Leank LSP server
"""

import asyncio, argparse, logging, os, sys
from asyncio import AbstractEventLoop, TaskGroup
from contextlib import suppress
from pathlib import Path

from .aio import DuplexStream
from .cli import split_cmd_line, version
from .jsonrpc import RpcMsgChannel, JsonRpcMsgStream, RpcChannel, RpcInterface
from .lake import LeankLakeFactory
from .server import (
    AsyncProgram,
    LspProgram,
    LspServer,
    RpcDirChannelFactory,
    RpcSubprocessFactory,
    async_stdio_main,
    channel_lsp_server,
)


class StdioProgram(LspProgram):
    def __init__(self, lake_cmd: list[str]):
        self._lake_cmd = lake_cmd

    async def start_server(self, client: RpcInterface, tg: TaskGroup) -> LspServer:
        loop = asyncio.get_running_loop()
        lake_factory = RpcSubprocessFactory(self._lake_cmd, loop)
        leank_factory = LeankLakeFactory(lake_factory)
        return channel_lsp_server(leank_factory, client, tg)


class LeankLakeConnection:
    def __init__(
        self, factory: RpcDirChannelFactory, tg: asyncio.TaskGroup, sock_path: Path
    ):
        self._factory = factory
        self._tg = tg
        self._loop = asyncio.get_running_loop()
        self._sock_path = sock_path
        self._connected = False

    def on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._connected:
            print(f"Editor is already connected to Lake workspace {self._sock_path}")
            return
        self._connected = True
        print(f"Editor connected to {self._sock_path}")
        aio = DuplexStream(reader, writer)
        sock_chan = RpcMsgChannel(JsonRpcMsgStream(aio, 'socket'), self._loop)
        self._tg.create_task(self._async_on_connect(sock_chan))

    async def _async_on_connect(self, sock_chan: RpcChannel) -> None:
        try:
            async with TaskGroup() as tg:
                server = channel_lsp_server(self._factory, sock_chan.proxy, tg)
                tg.create_task(sock_chan.pump(server))
        except Exception as ex:
            print(ex, file=sys.stderr)
        finally:
            self._connected = False
            print(f"Editor disconnected from {self._sock_path}")


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
                lake_factory = RpcSubprocessFactory(self._lake_cmd, loop)
                leank_factory = LeankLakeFactory(lake_factory)
                session = LeankLakeConnection(leank_factory, tg, sock_path)
                socket_server = await asyncio.start_unix_server(
                    session.on_connect, sock_path, backlog=1
                )
                async with socket_server:
                    print(f"Listening on {sock_path}...", flush=True)
                    await self._stdin_eof.wait()
                    socket_server.close()
                    print("Waiting for connections to finish...", flush=True)
                    await socket_server.wait_closed()
                    print("Connections done.", flush=True)
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
