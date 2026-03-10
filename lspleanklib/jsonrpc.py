"""
JSON-RPC
"""

from __future__ import annotations
import asyncio, enum, json, typing
from asyncio import Future
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from .aio import DuplexStream, MinimalReader, MinimalWriter
from .util import log


# Types from Language Server Protocol Specification
LspAny: TypeAlias = None | str | int | Sequence['LspAny'] | Mapping[str, 'LspAny']
LspObject: TypeAlias = Mapping[str, LspAny]
MsgParams: TypeAlias = Sequence[LspAny] | Mapping[str, LspAny]

class ErrorCodes(enum.IntEnum):
    UnknownErrorCode = -32001
    ServerNotInitialized = -32002


async def write_message(stream: MinimalWriter, msg: LspObject) -> None:
    body = json.dumps(msg, separators=(',', ':')).encode()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
    stream.write(header)
    stream.write(body)
    await stream.drain()


async def read_message(stream: MinimalReader) -> LspObject | None:
    body_length = 0
    try:
        while True:
            line = await stream.readuntil()
            key_val = line.split(b':', 2)
            if len(key_val) == 2:
                if key_val[0].lower() == b'content-length':
                    body_length = int(key_val[1].strip())
            else:
                # end of header (empty line if correct LSP)
                break
        body = await stream.readexactly(body_length)
        ret = json.loads(body)
        if not isinstance(ret, dict):
            raise ValueError("expecting JSON-RPC message to be JSON object")
        return typing.cast(LspObject, ret)
    except asyncio.exceptions.IncompleteReadError as ex:
        if ex.partial:
            raise ValueError("truncated stream input") from ex
        return None


@dataclass
class MethodCall:
    method: str
    params: MsgParams | None


@dataclass
class ResponseError:
    code: int
    message: str
    data: LspAny | None

    def __init__(self, msg: LspObject):
        code = msg.get('code')
        self.code = code if isinstance(code, int) else ErrorCodes.UnknownErrorCode
        self.message = str(msg.get('message'))
        self.data = msg.get('data')

    def as_lsp_obj(self) -> LspObject:
        return self.__dict__


@dataclass
class Response:
    result: LspAny
    error: ResponseError | None

    def __init__(self, msg: LspObject):
        error = msg.get('error')
        if error is None:
            self.result = msg.get('result')
            self.error = None
        elif not isinstance(error, dict):
            raise ValueError('LSP errors must be JSON objects')
        else:
            self.result = None
            self.error = ResponseError(error)

    @staticmethod
    def from_error_code(ec: ErrorCodes) -> Response:
        return Response({'error': ec, 'message': ec.name})


class JsonRpcMsg:
    id: int | str | None
    payload: MethodCall | Response

    def __init__(self, msg: LspObject):
        if msg.get('jsonrpc') != '2.0':
            raise ValueError('JSON object is not JSON-RPC 2.0 message')
        self.id = typing.cast(int | str | None, msg.get('id'))
        method = msg.get('method')
        if method is None:
            self.payload = Response(msg)
        elif not isinstance(method, str):
            raise ValueError('LSP method names must be strings')
        else:
            params = typing.cast(MsgParams | None, msg.get('params'))
            self.payload = MethodCall(method, params)


class ExpectedResponses:
    def __init__(self) -> None:
        self._todo: dict[int | str | None, Future[Response]] = {}
        self.next_id = 1

    def cancel_all(self) -> None:
        while self._todo:
            msg_id, expect = self._todo.popitem()
            expect.cancel()

    def prepare(self, fixed_id: str | None) -> tuple[int | str, Future[Response]]:
        msg_id = self.next_id if fixed_id is None else fixed_id
        if fixed_id is None:
            self.next_id += 1
        stale = self._todo.pop(msg_id, None)
        if stale is not None:
            log(f"Response abandoned due to id reuse by new request: {msg_id}")
            stale.cancel()
        expect: Future[Response] = asyncio.get_running_loop().create_future()
        self._todo[msg_id] = expect
        return (msg_id, expect)

    def got_response(self, response: Response, msg_id: int | str | None) -> None:
        expect = self._todo.pop(msg_id, None)
        if expect:
            expect.set_result(response)
        else:
            log(f"Unexpected response with id: {msg_id}")


class RpcInterface(typing.Protocol):
    async def notify(self, mc: MethodCall) -> None: ...
    async def request(
        self, mc: MethodCall, fix_id: str | None
    ) -> Awaitable[Response]: ...
    def release(self) -> None: ...


class RemoteRpcService(RpcInterface):
    def __init__(self, aout: MinimalWriter):
        self.aout = aout
        self.expected = ExpectedResponses()

    async def close(self) -> None:
        self.aout.close()
        await self.aout.wait_closed()

    async def notify(self, mc: MethodCall) -> None:
        await self._write_jsonrpc(method=mc.method, params=mc.params)

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        (msg_id, expect) = self.expected.prepare(fix_id)
        await self._write_jsonrpc(id=msg_id, method=mc.method, params=mc.params)
        return expect

    def release(self) -> None:
        self.aout.close()

    async def response(
        self, tbd: Awaitable[Response], msg_id: int | str | None
    ) -> None:
        try:
            response = await tbd
            if response.error is not None:
                await self._write_jsonrpc(id=msg_id, error=response.error.as_lsp_obj())
            else:
                await self._write_jsonrpc(id=msg_id, result=response.result)
        except asyncio.CancelledError as ex:
            log(ex)

    async def _write_jsonrpc(self, **kwargs: LspAny) -> None:
        await write_message(self.aout, dict(jsonrpc='2.0', **kwargs))


class JsonRpcDuplexConnection:
    def __init__(self, aio: DuplexStream):
        self._ain = aio.ain
        self.remote = RemoteRpcService(aio.aout)

    async def run(self, impl: RpcInterface) -> bool:
        """Listen for JSONRPC message on stream input until stream EOF.

        impl: implements the methods for RPC calls received on stream input
        """
        async with asyncio.TaskGroup() as tg:
            try:
                async for msg in self._stream_jsonrpc():
                    if isinstance(msg.payload, Response):
                        self.remote.expected.got_response(msg.payload, msg.id)
                    elif msg.id is None:
                        await impl.notify(msg.payload)
                    else:
                        fix_id = msg.id if isinstance(msg.id, str) else None
                        tbd_response = await impl.request(msg.payload, fix_id)
                        tg.create_task(self.remote.response(tbd_response, msg.id))
                return True
            except ValueError as ex:
                log(ex)
                return False
            except IOError as ex:
                log(ex)
                return False
            finally:
                self.remote.expected.cancel_all()
                impl.release()

    async def _stream_jsonrpc(self) -> AsyncIterator[JsonRpcMsg]:
        while (msg := await read_message(self._ain)) is not None:
            try:
                yield JsonRpcMsg(msg)
            except ValueError as ex:
                log(ex)
