import pytest

import asyncio, contextlib, os, shutil
from pathlib import Path

from lspleanklib.lspleank import LeankSubprocessFactory, server_loop
from lspleanklib.jsonrpc import (
    JsonRpcDuplexConnection,
    MethodCall as MC,
    Response,
    RpcInterface,
)

from util import MockEditor, aio_xpipe, initialize_call

# using `pytestmark` to skip this entire module
pytestmark = pytest.mark.skipif(
    shutil.which('lake') is None, reason='lake not found'
)

CASES_DIR = Path(__file__).parent / "cases"
INIT_BYTES = (CASES_DIR / "vim9-lsp-init.txt").read_bytes()

LAKE_CMD = ["lake", "serve"]
if os.environ.get('SNIFF_LSP'):
    LAKE_CMD = ["lsp-devtools", "agent", "--"] + LAKE_CMD


@contextlib.asynccontextmanager
async def server_session(editor_impl: RpcInterface, server_cmd: list[str]):
    async with aio_xpipe() as (outer, inner):
        factory = LeankSubprocessFactory(server_cmd)
        server_loop_task = asyncio.create_task(server_loop(inner, factory))
        con = JsonRpcDuplexConnection(outer, 'editor')
        client_pump_task = asyncio.create_task(con.pump(editor_impl))
        yield con.proxy
        outer.aout.close()
        assert await server_loop_task == 0
        assert await client_pump_task == True

@contextlib.asynccontextmanager
async def server_session_init(client: RpcInterface, server_cmd: list[str], root: Path):
    async with server_session(client, LAKE_CMD) as rpc:
        aw_resp = await rpc.request(initialize_call(root))
        server_init = await aw_resp
        assert server_init.result['serverInfo']['name'] == "lspleank"
        await rpc.notify(MC('initialized', {}))
        yield rpc
        aw_resp = await rpc.request(MC('shutdown'))
        assert await aw_resp == Response(None)
        await rpc.notify(MC('exit'))


async def test_sub_lake():
    rootPath = CASES_DIR / "min_import"
    with contextlib.chdir(rootPath):
        async with server_session_init(MockEditor(), LAKE_CMD, rootPath):
            pass


MIN_IMPORT_CASE = CASES_DIR / 'min_import'


def didOpen_call(doc_path):
    return MC('textDocument/didOpen', {
        'textDocument': {
            'uri': doc_path.as_uri(),
            'version': 1,
            'languageId': 'lean',
            'text': doc_path.read_text(),
        }
    })


@pytest.mark.slow
@pytest.mark.parametrize('rootPath', [
    CASES_DIR / 'min_import',
    CASES_DIR,
])
async def test_open_doc(rootPath):
    with contextlib.chdir(rootPath):
        editor = MockEditor()
        async with server_session_init(editor, LAKE_CMD, rootPath) as rpc:
            main_path = MIN_IMPORT_CASE / 'Main.lean'
            await rpc.notify(didOpen_call(main_path))

            pending = editor.future_notif('textDocument/publishDiagnostics')
            notif = await pending
            assert notif['uri'] == main_path.as_uri()
            assert notif['diagnostics'] == []
