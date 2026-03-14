import pytest

import asyncio, contextlib, os, shutil
from pathlib import Path

from lspleanklib.lspleank import SubprocessLeankFactory, server_loop
from lspleanklib.jsonrpc import (
    JsonRpcDuplexConnection,
    MethodCall as MC,
    Response,
)

from util import MockClient, aio_xpipe, initialize_call

# using `pytestmark` to skip this entire module
pytestmark = pytest.mark.skipif(
    shutil.which('lake') is None, reason='lake not found'
)

CASES_DIR = Path(__file__).parent / "cases"
INIT_BYTES = (CASES_DIR / "vim9-lsp-init.txt").read_bytes()

LAKE_CMD = ["lake", "serve"]
if os.environ.get('SNIFF_LSP'):
    LAKE_CMD = ["lsp-devtools", "agent", "--"] + LAKE_CMD


@pytest.mark.slow
async def test_sub_lake():
    rootPath = CASES_DIR / "min_import/"
    with contextlib.chdir(rootPath):
        async with aio_xpipe() as (outer, inner):
            factory = SubprocessLeankFactory(LAKE_CMD)
            server_loop_task = asyncio.create_task(server_loop(inner, factory))

            con = JsonRpcDuplexConnection(outer, 'outer')
            client_pump_task = asyncio.create_task(con.pump(MockClient()))

            tbd = await con.proxy.request(initialize_call(rootPath))
            server_init = await tbd
            assert server_init.result['serverInfo']['name'] == "Lean 4 Server"

            await con.proxy.notify(MC('initialized', {}))

            tbd = await con.proxy.request(MC('shutdown', None))
            assert await tbd == Response(None)

            await con.proxy.notify(MC('exit', None))
            outer.aout.close()

            assert await server_loop_task == 0
            assert await client_pump_task == True
