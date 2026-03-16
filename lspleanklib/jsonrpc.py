"""
JSON-RPC
"""

from __future__ import annotations
import asyncio, enum, json, typing
from asyncio import Future
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeAlias
from warnings import warn

from .aio import DuplexStream, MinimalReader, MinimalWriter
from .util import log


# Types from Language Server Protocol Specification
LspAny: TypeAlias = None | str | int | Sequence['LspAny'] | Mapping[str, 'LspAny']
LspObject: TypeAlias = Mapping[str, LspAny]
MsgParams: TypeAlias = Sequence[LspAny] | Mapping[str, LspAny]


class ErrorCodes(enum.IntEnum):
    UnknownErrorCode = -32001
    ServerNotInitialized = -32002
    InvalidRequest = -32600
    InternalError = -32603


async def write_message(stream: MinimalWriter, msg: Mapping[str, Any]) -> None:
    body = json.dumps(msg, separators=(',', ':')).encode()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
    stream.write(header)
    stream.write(body)
    await stream.drain()


async def read_message(stream: MinimalReader) -> dict[str, Any] | None:
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
        return ret
    except asyncio.exceptions.IncompleteReadError as ex:
        if ex.partial:
            raise ValueError("truncated stream input") from ex
        return None


@dataclass
class MethodCall:
    method: str
    params: MsgParams | None = None

    def to_lsp_obj(self) -> LspObject:
        return asdict(self)


@dataclass
class ResponseError:
    code: int
    message: str
    data: LspAny | None = None

    @staticmethod
    def from_lsp_obj(msg: LspObject) -> ResponseError:
        code = msg.get('code')
        if not isinstance(code, int):
            code = ErrorCodes.UnknownErrorCode
        return ResponseError(code, str(msg.get('message')), msg.get('data'))


async def future_error(ec: ErrorCodes) -> Response:
    return Response.from_error_code(ec)


@dataclass
class Response:
    result: LspAny
    error: ResponseError | None = None

    @staticmethod
    def from_lsp_obj(msg: LspObject) -> Response:
        error = msg.get('error')
        if error is None:
            return Response(msg.get('result'))
        elif not isinstance(error, dict):
            raise ValueError('LSP errors must be JSON objects')
        else:
            return Response(None, ResponseError.from_lsp_obj(error))

    @staticmethod
    def from_error_code(ec: ErrorCodes) -> Response:
        return Response(None, ResponseError(ec, ec.name))

    def to_lsp_obj(self) -> LspObject:
        if self.error:
            return {'error': asdict(self.error)}
        else:
            return {'result': self.result}


@dataclass
class JsonRpcMsg:
    payload: MethodCall | Response
    id: int | str | None = None

    @staticmethod
    def from_jsonrpc(msg: LspObject) -> JsonRpcMsg:
        if msg.get('jsonrpc') != '2.0':
            raise ValueError('JSON object is not JSON-RPC 2.0 message')
        msg_id = typing.cast(int | str | None, msg.get('id'))
        method = msg.get('method')
        if method is None:
            return JsonRpcMsg(Response.from_lsp_obj(msg), msg_id)
        elif not isinstance(method, str):
            raise ValueError('LSP method names must be strings')
        else:
            params = typing.cast(MsgParams | None, msg.get('params'))
            return JsonRpcMsg(MethodCall(method, params), msg_id)

    def to_lsp_obj(self) -> LspObject:
        ret: dict[str, LspAny] = {} if self.id is None else {'id': self.id}
        ret.update(self.payload.to_lsp_obj())
        ret['jsonrpc'] = '2.0'
        return ret


async def write_jsonrpc(aout: MinimalWriter, msg: JsonRpcMsg) -> None:
    await write_message(aout, msg.to_lsp_obj())


class IncommingResponses:
    def __init__(self) -> None:
        self._todo: dict[int | str | None, Future[Response]] = {}
        self.next_id = 1

    def __bool__(self) -> bool:
        return bool(self._todo)

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
            warn(f"Response abandoned due to id reuse by new request: {msg_id}")
            stale.cancel()
        expect: Future[Response] = asyncio.get_running_loop().create_future()
        self._todo[msg_id] = expect
        return (msg_id, expect)

    def got_response(self, response: Response, msg_id: int | str | None) -> None:
        expect = self._todo.pop(msg_id, None)
        if expect:
            expect.set_result(response)
        else:
            warn(f"Unexpected response with id: {msg_id}")


class RpcInterface(typing.Protocol):
    async def notify(self, mc: MethodCall) -> None: ...
    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]: ...


class RemoteRpcProxy(RpcInterface):
    def __init__(self, aout: MinimalWriter):
        self._aout = aout
        self._expecting: IncommingResponses | None = IncommingResponses()

    async def notify(self, mc: MethodCall) -> None:
        if self._aout.is_closing():
            warn('notify called on closed or closing RPC connection')
            return
        await write_jsonrpc(self._aout, JsonRpcMsg(mc))

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if self._expecting is None or self._aout.is_closing():
            warn('request called on closed or closing RPC connection')
            return future_error(ErrorCodes.InternalError)
        (msg_id, expect) = self._expecting.prepare(fix_id)
        await write_jsonrpc(self._aout, JsonRpcMsg(mc, msg_id))
        return expect

    def got_response(self, response: Response, msg_id: int | str | None) -> None:
        if self._expecting is None:
            warn('RemoteRpcProxy.got_response called after end_of_incomming_responses')
        else:
            self._expecting.got_response(response, msg_id)

    def end_of_incomming_responses(self) -> None:
        if self._expecting:
            self._expecting.cancel_all()
            log.info("orphaned incomming responses cancelled")
        self._expecting = None


async def await_send_response(
    aout: MinimalWriter, tbd: Awaitable[Response], msg_id: int | str | None
) -> None:
    try:
        response = await tbd
        await write_jsonrpc(aout, JsonRpcMsg(response, msg_id))
    except asyncio.CancelledError as ex:
        log.exception(ex)


class RequestResponseHelper:
    def __init__(self, aout: MinimalWriter, impl: RpcInterface, tg: asyncio.TaskGroup):
        self._aout = aout
        self._impl = impl
        self._tg = tg

    async def request(self, msg_id: int | str | None, mc: MethodCall) -> None:
        fix_id = msg_id if isinstance(msg_id, str) else None
        tbd_response = await self._impl.request(mc, fix_id)
        self._tg.create_task(await_send_response(self._aout, tbd_response, msg_id))


class JsonRpcDuplexConnection:
    def __init__(self, aio: DuplexStream, name: str):
        self._aio = aio
        self.proxy = RemoteRpcProxy(aio.aout)
        self.name = name

    async def pump(self, impl: RpcInterface) -> bool:
        """Listen for JSONRPC message on stream input until stream EOF.

        impl: implements the methods for RPC calls received on stream input
        """
        try:
            async with asyncio.TaskGroup() as tg:
                helper = RequestResponseHelper(self._aio.aout, impl, tg)
                try:
                    async for msg in self._stream_jsonrpc():
                        if isinstance(msg.payload, Response):
                            self.proxy.got_response(msg.payload, msg.id)
                        elif msg.id is None:
                            await impl.notify(msg.payload)
                        else:
                            await helper.request(msg.id, msg.payload)
                    return True
                except (ValueError, IOError) as ex:
                    log.exception(ex)
                    return False
                finally:
                    self.proxy.end_of_incomming_responses()
                    log.debug(f"{self.name} pump done reading responses")
        finally:
            self._aio.aout.close()
            log.debug(f"{self.name} pump closing output stream")

    async def _stream_jsonrpc(self) -> AsyncIterator[JsonRpcMsg]:
        while (msg := await read_message(self._aio.ain)) is not None:
            try:
                yield JsonRpcMsg.from_jsonrpc(msg)
            except ValueError as ex:
                log.exception(ex)
