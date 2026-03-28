import pytest

import asyncio, contextlib, os, shutil
from pathlib import Path

from lspleanklib.jsonrpc import (
    RpcMsgChannel,
    JsonRpcMsgStream,
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


from util import ok_server_loop


@contextlib.asynccontextmanager
async def server_session(editor_impl: RpcInterface, server_cmd: list[str]):
    loop = asyncio.get_running_loop()
    async with aio_xpipe() as (outer, inner):
        outer_chan = RpcMsgChannel(JsonRpcMsgStream(outer), name='outer', loop=loop)
        client_pump_task = asyncio.create_task(outer_chan.pump(editor_impl))
        server_task = asyncio.create_task(ok_server_loop(inner, server_cmd))
        yield outer_chan.proxy
        assert await server_task == True
        await client_pump_task
        assert not client_pump_task.exception()


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
        await rpc.close_and_wait()


async def test_sub_lake():
    rootPath = CASES_DIR / "min_import"
    with contextlib.chdir(rootPath):
        async with server_session_init(MockEditor(), LAKE_CMD, rootPath):
            pass


MIN_IMPORT_CASE = CASES_DIR / 'min_import'
ALT_IMPORT_CASE = CASES_DIR / 'alt_import'


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


@pytest.mark.slow
async def test_workspace_symbol_search():
    rootPath = CASES_DIR
    with contextlib.chdir(rootPath):
        editor = MockEditor()
        async with server_session_init(editor, LAKE_CMD, rootPath) as rpc:
            min_main_path = MIN_IMPORT_CASE / 'Main.lean'
            alt_main_path = ALT_IMPORT_CASE / 'Main.lean'
            await rpc.notify(didOpen_call(min_main_path))
            await rpc.notify(didOpen_call(alt_main_path))

            while True:
                pending = editor.future_notif('textDocument/publishDiagnostics')
                notif = await pending
                if notif['uri'] == alt_main_path.as_uri():
                    break

            aw_response = await rpc.request(MC('workspace/symbol', {'query': 'foobarsical'}))
            response = await aw_response
            assert response.result is not None
            assert len(response.result) == 2
            assert response.result[0]['name'] == 'foobarsical'
            assert response.result[0]['location']['uri'] == (MIN_IMPORT_CASE / 'Min.lean').as_uri()
            assert response.result[1]['name'] == 'foobarsical'
            assert response.result[1]['location']['uri'] == (ALT_IMPORT_CASE / 'Min.lean').as_uri()
