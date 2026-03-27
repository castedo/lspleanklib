"""
JSON-RPC for Language Server Protocol
"""

from __future__ import annotations
import asyncio, copy, enum, json, typing
from asyncio import AbstractEventLoop, Future, TaskGroup
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeAlias
from warnings import warn

from .aio import DuplexStream, MinimalReader, MinimalWriter
from .util import LspAny, LspObject, log


class ErrorCodes(enum.IntEnum):
    UnknownErrorCode = -32001
    ServerNotInitialized = -32002
    InvalidRequest = -32600
    MethodNotFound = -32601
    InvalidParams = -32602
    InternalError = -32603
    RequestFailed = -32803


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


MsgParams: TypeAlias = Sequence[LspAny] | Mapping[str, LspAny]


@dataclass
class MethodCall:
    method: str
    params: MsgParams | None = None

    def to_lsp_obj(self) -> LspObject:
        lobj: dict[str, LspAny] = {'method': self.method}
        if self.params is not None:
            lobj['params'] = copy.deepcopy(self.params)
        return lobj


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
    def __init__(self, loop: AbstractEventLoop) -> None:
        self._loop = loop
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
        expect: Future[Response] = self._loop.create_future()
        self._todo[msg_id] = expect
        return (msg_id, expect)

    def got_response(self, response: Response, msg_id: int | str | None) -> None:
        expect = self._todo.pop(msg_id, None)
        if expect:
            expect.set_result(response)
        else:
            warn(f"Unexpected response with id: {msg_id}")


class RpcInterface(typing.Protocol):
    def close(self) -> None: ...
    async def notify(self, mc: MethodCall) -> None: ...
    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]: ...


class RemoteRpcProxy(RpcInterface):
    def __init__(self, aout: MinimalWriter, loop: AbstractEventLoop):
        self._aout = aout
        self._expecting: IncommingResponses | None = IncommingResponses(loop)

    def close(self) -> None:
        log.debug(f"closing {self.__class__.__name__}")
        self._aout.close()

    async def notify(self, mc: MethodCall) -> None:
        if self._aout.is_closing():
            log.info(f"notification '{mc.method}' on closed or closing RPC connection")
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
            warn('RemoteRpcProxy.got_response called after end_of_incoming_responses')
        else:
            self._expecting.got_response(response, msg_id)

    def end_of_incomming_responses(self) -> None:
        if self._expecting:
            self._expecting.cancel_all()
            log.info("orphaned incoming responses cancelled")
        self._expecting = None


class RpcChannel(typing.Protocol):
    @property
    def proxy(self) -> RpcInterface: ...

    async def pump(self, impl: RpcInterface) -> None: ...


class NoClient(RpcInterface):
    async def notify(self, mc: MethodCall) -> None:
        warn(f"No client RPC implementation for '{mc.method}' notification")

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        warn(f"No client RPC implementation for '{mc.method}' request")
        return future_error(ErrorCodes.MethodNotFound)

    def close(self) -> None:
        pass


async def await_send_response(
    aout: MinimalWriter, tbd: Awaitable[Response], msg_id: int | str | None
) -> None:
    try:
        response = await tbd
        await write_jsonrpc(aout, JsonRpcMsg(response, msg_id))
    except (asyncio.CancelledError, ValueError) as ex:
        # writing to closed aout stream raises ValueError
        log.exception(ex)


class JsonRpcChannel(RpcChannel):
    def __init__(self, aio: DuplexStream, loop: AbstractEventLoop, name: str):
        self._aio = aio
        self._proxy = RemoteRpcProxy(aio.aout, loop)
        self.name = name

    @property
    def proxy(self) -> RpcInterface:
        return self._proxy

    async def pump(self, impl: RpcInterface = NoClient()) -> None:
        """Listen for JSONRPC message on stream input until stream EOF.

        impl: implements the methods for RPC calls received on stream input
        """
        try:
            async with TaskGroup() as response_tasks:
                try:
                    async for msg in self._stream_jsonrpc():
                        if isinstance(msg.payload, Response):
                            self._proxy.got_response(msg.payload, msg.id)
                        elif msg.id is None:
                            await impl.notify(msg.payload)
                        else:
                            fix_id = msg.id if isinstance(msg.id, str) else None
                            tbd = await impl.request(msg.payload, fix_id)
                            coro = await_send_response(self._aio.aout, tbd, msg.id)
                            response_tasks.create_task(coro)
                finally:
                    impl.close()
                    self._proxy.end_of_incomming_responses()
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
