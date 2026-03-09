"""
CLI common code
"""

import asyncio, threading
from asyncio import AbstractEventLoop
from collections.abc import Awaitable


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


def split_cmd_line(
    cmd_line_args: list[str], default_extra_args: list[str]
) -> tuple[list[str], list[str]]:
    try:
        cut = cmd_line_args.index('--')
    except ValueError:
        cut = len(cmd_line_args)
    extra_args = cmd_line_args[cut + 1 :]
    return (cmd_line_args[:cut], extra_args or default_extra_args)
