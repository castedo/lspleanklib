import pytest

import asyncio, contextlib, logging, os, shutil, sys
from pathlib import Path
from typing import Any, BinaryIO

from lspleanklib.aio import (
    DuplexStream,
    MinimalReader,
    MinimalWriter,
    WriterFileAdapter,
)
from lspleanklib.jsonrpc import (
    JsonRpcDuplexChannel,
    JsonRpcMsg,
    LspObject,
    MethodCall,
    read_message,
    write_jsonrpc,
    write_message,
)

from util import aio_xpipe, ok_server_loop, pipe_stream_reader


skipif_no_pylsp = pytest.mark.skipif(
    shutil.which('pylsp') is None, reason='pylsp not found'
)

TESTS_DIR = Path(__file__).parent
TEST_CASES = TESTS_DIR / "cases"
INIT_BYTES = (TEST_CASES / "vim9-lsp-init.txt").read_bytes()


async def write_notify(aout, method, params) -> None:
    await write_jsonrpc(aout, JsonRpcMsg(MethodCall(method, params)))

async def write_request(aout, method, params, id) -> None:
    await write_jsonrpc(aout, JsonRpcMsg(MethodCall(method, params), id))

async def readexactly(f: BinaryIO, n: int) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, f.read, n)


@contextlib.asynccontextmanager
async def a_pipe():
    loop = asyncio.get_running_loop()
    r, w = os.pipe()
    with os.fdopen(w, 'wb') as fw:
        with os.fdopen(r, 'rb') as fr:
            reader = await pipe_stream_reader(fr, loop)
            writer = WriterFileAdapter(fw, loop)
            yield DuplexStream(reader, writer)


async def msg_loop(super_io: DuplexStream, sub_io: DuplexStream) -> None:
    super_con = JsonRpcDuplexChannel(super_io, 'super')
    sub_con = JsonRpcDuplexChannel(sub_io, 'sub')
    async with asyncio.TaskGroup() as tg:
        tg.create_task(sub_con.pump(super_con.proxy))
        tg.create_task(super_con.pump(sub_con.proxy))


@pytest.fixture
async def msg_loop_trio():
    async with aio_xpipe() as (super_outer, super_inner):
        async with aio_xpipe() as (sub_outer, sub_inner):
            ta = asyncio.create_task(msg_loop(super_inner, sub_outer))
            yield (super_outer, sub_inner, ta)


async def test_init_msg():
    async with a_pipe() as p:
        p.aout.write(INIT_BYTES)
        await p.aout.drain()
        msg = await read_message(p.ain)
        assert msg['id'] == 1
        assert msg['method'] == 'initialize'

        await write_message(p.aout, msg)
        lines = INIT_BYTES.splitlines(keepends=True)
        assert await p.ain.readuntil() == lines[0]
        assert await p.ain.readuntil() == lines[1]
        p.aout.write(b'\n')
        await p.aout.drain()
        assert await p.ain.readuntil() == lines[2] + b'\n'


async def test_empty_stdin(msg_loop_trio):
    (outer, inner, taloop) = msg_loop_trio
    outer.aout.close()
    inner.aout.close()
    await taloop


async def assert_echo(aout: MinimalWriter, ain: MinimalReader, msg: LspObject) -> None:
    await write_message(aout, msg)
    assert await read_message(ain) == msg


async def assert_init(outer: DuplexStream, inner: DuplexStream) -> None:
    outer.aout.write(INIT_BYTES)
    await outer.aout.drain()
    msg: Any = await read_message(inner.ain)
    assert msg is not None
    assert msg['id'] == 1
    assert msg['method'] == 'initialize'
    assert msg['params']['clientInfo']['name'] == 'Vim'

    await assert_echo(inner.aout, outer.ain, {
        "id": 1,
        "jsonrpc": "2.0",
        "result": {
            "capabilities": {"documentHighlightProvider": True},
            "serverInfo": {"name": "Leanish 0 Server", "version": "0.0.0"}
        }
    })

    await assert_echo(outer.aout, inner.ain, {
        "jsonrpc": "2.0", "method": "initialized", "params": {}
    })


async def test_register_lean_watcher(msg_loop_trio):
    (outer, inner, taloop) = msg_loop_trio
    await assert_init(outer, inner)
    await assert_echo(inner.aout, outer.ain, {
        "id": "register_lean_watcher",
        "jsonrpc": "2.0",
        "method": "client/registerCapability",
        "params": {"registrations": []}
    })
    await assert_echo(outer.aout, inner.ain, {
        "id": "register_lean_watcher",
        "jsonrpc": "2.0",
        "result": None,
    })
    outer.aout.close()
    inner.aout.close()
    await taloop


async def test_definition(msg_loop_trio, capsys):
    (outer, inner, taloop) = msg_loop_trio
    await assert_init(outer, inner)
    await assert_echo(outer.aout, inner.ain, {
        "method": "textDocument/didOpen",
        "jsonrpc": "2.0",
        "params": {
            "textDocument": {
                "uri": "file:///tmp/min_import/Main.lean",
                "version": 3,
                "languageId": "lean",
                "text": "import Min\n#eval foo\n"
            }
        }
    })
    await assert_echo(outer.aout, inner.ain, {
        "method":"textDocument/definition",
        "jsonrpc":"2.0",
        "id": 2,
        "params": {
            "textDocument": {"uri": "file:///tmp/min_import/Main.lean"},
            "position":{"character": 6, "line": 1}
        }
    })
    await assert_echo(inner.aout, outer.ain, {
        "id": 2,
        "jsonrpc":"2.0",
        "result":[{
            "originSelectionRange": {"end": {"character": 9, "line": 1}, "start": {"character": 6,"line": 1}},
            "targetRange": {"end": {"character": 7, "line": 0}, "start": {"character": 4, "line": 0}},
            "targetSelectionRange": {"end": {"character": 7, "line": 0}, "start": {"character": 4, "line": 0}},
            "targetUri":"file:///temp/min_import/Min.lean"
        }]
    })
    outer.aout.close()
    inner.aout.close()
    await taloop
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


async def test_empty_stdin_subproc() -> None:
    async with aio_xpipe() as (outer, inner):
        tloop = asyncio.create_task(ok_server_loop(inner, ["cat"]))
        outer.aout.close()
        assert await tloop == False


@skipif_no_pylsp
@pytest.mark.slow
async def test_loop_pylsp():
    async with aio_xpipe() as (outer, inner):
        tloop = asyncio.create_task(ok_server_loop(inner, ["pylsp"]))
        outer.aout.write(INIT_BYTES)
        await outer.aout.drain()
        msg = await read_message(outer.ain)
        assert msg['id'] == 1
        assert msg['result']['serverInfo']['name'] == "lspleank"
        await write_notify(outer.aout, "initialized", {})
        await write_request(outer.aout, "shutdown", None, 2)
        await write_notify(outer.aout, "exit", None)
        outer.aout.close()
        assert await tloop == True


@skipif_no_pylsp
@pytest.mark.slow
async def test_aborted_initialization():
    async with aio_xpipe() as (outer, inner):
        tloop = asyncio.create_task(ok_server_loop(inner, ["pylsp"]))
        outer.aout.write(INIT_BYTES)
        await outer.aout.drain()
        await read_message(outer.ain)
        outer.aout.close()
        assert await tloop == False


@skipif_no_pylsp
@pytest.mark.slow
async def test_eof_without_shutdown():
    async with aio_xpipe() as (outer, inner):
        tloop = asyncio.create_task(ok_server_loop(inner, ["pylsp"]))
        outer.aout.write(INIT_BYTES)
        await outer.aout.drain()
        await read_message(outer.ain)
        await write_notify(outer.aout, "initialized", {})
        outer.aout.close()
        assert await tloop == True


@skipif_no_pylsp
@pytest.mark.slow
async def test_eof_without_exit():
    async with aio_xpipe() as (outer, inner):
        tloop = asyncio.create_task(ok_server_loop(inner, ["pylsp"]))
        outer.aout.write(INIT_BYTES)
        await outer.aout.drain()
        await read_message(outer.ain)
        await write_notify(outer.aout, "initialized", {})
        await write_request(outer.aout, "shutdown", None, 2)
        outer.aout.close()
        assert await tloop == True


async def test_bogus_stdin(caplog, capsys) -> None:
    caplog.set_level(logging.CRITICAL)
    async with aio_xpipe() as (outer, inner):
        tloop = asyncio.create_task(ok_server_loop(inner, ["cat"]))
        outer.aout.write(b"BOGUS")
        await outer.aout.drain()
        outer.aout.close()
        assert await tloop == False
        assert await outer.ain.read() == b''
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


@skipif_no_pylsp
@pytest.mark.slow
@pytest.mark.parametrize("cmdline", [
    ["pylsp"],
    [sys.executable, "-m", "lspleanklib", "stdio", "--", "pylsp"],
])
async def test_sub_exec_pylsp(cmdline):
    proc = await asyncio.create_subprocess_exec(
        *cmdline, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    proc.stdin.write(INIT_BYTES)
    await proc.stdin.drain()
    msg = await read_message(proc.stdout)
    assert msg['id'] == 1
    assert msg['result']['serverInfo']['name'] in {"pylsp", "lspleank"}
    await write_notify(proc.stdin, "initialized", {})
    await write_request(proc.stdin, "shutdown", None, 2)
    await write_notify(proc.stdin, "exit", None)
    await proc.communicate()
    assert proc.returncode == 0
