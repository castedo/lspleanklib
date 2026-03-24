"""
Adapt Lake LSP server to be a Leank LSP server
"""

import argparse, logging, sys
from asyncio import AbstractEventLoop
from pathlib import Path

from .cli import split_cmd_line, version
from .lake import LeankLakeSession
from .server import (
    LspProgram,
    LspSession,
    RpcSubprocess,
    async_stdio_main,
)


class LakeLspOut(LspProgram):
    command: str
    lake_cmd: list[str]

    def __init__(self, cmd_line_args: list[str]):
        cli = argparse.ArgumentParser(prog='lakelspout', description=__doc__)
        cli.add_argument('--version', action='version', version=version())
        cli.add_argument('command', choices=['stdio'])

        (args, self.lake_cmd) = split_cmd_line(cmd_line_args, ['lake', 'serve'])
        cli.parse_args(args, self)

    async def aget_session(self, loop: AbstractEventLoop) -> LspSession:
        lake_chan = await RpcSubprocess.anew(self.lake_cmd, Path.cwd(), loop)
        return LeankLakeSession(lake_chan)


def main(cmd_line_args: list[str] | None = None) -> int:
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]
    logging.basicConfig()
    logging.captureWarnings(True)
    return async_stdio_main(LakeLspOut(cmd_line_args))


if __name__ == '__main__':
    exit(main())
