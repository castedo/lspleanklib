"""
Adapt a Lake LSP server as a Leank LSP server.
"""

import asyncio, argparse, logging, sys
from asyncio import TaskGroup

from .cli import split_cmd_line, version
from .jsonrpc import RpcInterface
from .lake import LeankLakeFactory
from .server import (
    AsyncProgram,
    LspProgram,
    LspServer,
    RpcSubprocessFactory,
    async_stdio_main,
    channel_lsp_server,
)


class StdioProgram(LspProgram):
    def __init__(self, lake_cmd: list[str]):
        super().__init__()
        self._lake_cmd = lake_cmd

    async def start_server(self, client: RpcInterface, tg: TaskGroup) -> LspServer:
        loop = asyncio.get_running_loop()
        lake_factory = RpcSubprocessFactory(self._lake_cmd, loop=loop)
        leank_factory = LeankLakeFactory(lake_factory)
        return channel_lsp_server(leank_factory, client, tg)


def main(cmd_line_args: list[str] | None = None) -> int:
    logging.basicConfig()
    logging.captureWarnings(True)

    cli = argparse.ArgumentParser(
        prog='lakelspout',
        description=__doc__,
        usage=(
            "%(prog)s  [-h] [--version] {stdio}"
            " [-- lake_serve_command ...]"
        ),
    )
    cli.add_argument('--version', action='version', version=version())
    sub = cli.add_subparsers(dest='subcmd', required=True)

    sub.add_parser(
        'stdio',
        help='run as a stdio Leank LSP server',
    )

    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    (cli_args, extra_args) = split_cmd_line(cmd_line_args, ['lake', 'serve'])
    args = cli.parse_args(cli_args)

    aprog: AsyncProgram
    match args.subcmd:
        case 'stdio':
            aprog = StdioProgram(extra_args)
    return async_stdio_main(aprog)


if __name__ == '__main__':
    exit(main())
