"""
JSON-RPC for Language Server Protocol
"""

from __future__ import annotations
import asyncio, copy, datetime, enum, json, typing
from asyncio import AbstractEventLoop, Future, TaskGroup
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TextIO, TypeAlias
from warnings import warn

from .aio import DuplexStream, MinimalReader, MinimalWriter
from .util import LspAny, LspObject, log


class ErrorCode(enum.IntEnum):
    UnknownErrorCode = -32001
    ServerNotInitialized = -32002
    InvalidRequest = -32600
    MethodNotFound = -32601
    InvalidParams = -32602
    InternalError = -32603
    RequestFailed = -32803


ErrorCodes = ErrorCode  # in the LSP specification the name is unpythonic plural


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
            code = ErrorCode.UnknownErrorCode
        return ResponseError(code, str(msg.get('message')), msg.get('data'))


async def awaitable_error(ec: ErrorCode) -> Response:
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
    def from_error_code(ec: ErrorCode) -> Response:
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


class RpcMsgConnection(typing.Protocol):
    async def close_and_wait(self) -> None: ...
    async def write(self, msg: JsonRpcMsg) -> None: ...
    async def read(self) -> JsonRpcMsg | None: ...


class JsonRpcMsgStream(RpcMsgConnection):
    def __init__(self, aio: DuplexStream):
        self._aio = aio

    async def close_and_wait(self) -> None:
        self._aio.aout.close()
        await self._aio.aout.wait_closed()

    async def write(self, msg: JsonRpcMsg) -> None:
        await write_message(self._aio.aout, msg.to_lsp_obj())

    async def read(self) -> JsonRpcMsg | None:
        while (msg := await read_message(self._aio.ain)) is not None:
            try:
                return JsonRpcMsg.from_jsonrpc(msg)
            except ValueError as ex:
                log.exception(ex)
        return None


class RpcInterface(typing.Protocol):
    async def close_and_wait(self) -> None: ...
    async def notify(self, mc: MethodCall) -> None: ...
    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]: ...


class RemoteRpcProxy(RpcInterface):
    def __init__(self, conn: RpcMsgConnection, *, loop: AbstractEventLoop):
        self._conn = conn
        self._expecting: IncommingResponses | None = IncommingResponses(loop)

    async def close_and_wait(self) -> None:
        log.debug(f"closing {self.__class__.__name__}")
        await self._conn.close_and_wait()

    async def notify(self, mc: MethodCall) -> None:
        msg = JsonRpcMsg(mc)
        try:
            await self._conn.write(msg)
        except RuntimeError:
            log.exception(f"Write failed for RPC call '{mc.method}'")

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        if self._expecting is None:
            warn('request called on closed or closing RPC connection')
            return awaitable_error(ErrorCode.InternalError)
        (msg_id, expect) = self._expecting.prepare(fix_id)
        try:
            await self._conn.write(JsonRpcMsg(mc, msg_id))
        except RuntimeError:
            error_response = Response.from_error_code(ErrorCode.InternalError)
            self._expecting.got_response(error_response, msg_id)
            log.exception(f"Write failed for RPC call '{mc.method}'")
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

    async def pump(self, impl: RpcInterface | None = None) -> None: ...


class NoClient(RpcInterface):
    async def notify(self, mc: MethodCall) -> None:
        warn(f"No client RPC implementation for '{mc.method}' notification")

    async def request(
        self, mc: MethodCall, fix_id: str | None = None
    ) -> Awaitable[Response]:
        warn(f"No client RPC implementation for '{mc.method}' request")
        return awaitable_error(ErrorCode.MethodNotFound)

    async def close_and_wait(self) -> None:
        pass


async def await_send_response(
    conn: RpcMsgConnection, tbd: Awaitable[Response], msg_id: int | str | None
) -> None:
    try:
        response = await tbd
        await conn.write(JsonRpcMsg(response, msg_id))
    except (asyncio.CancelledError, ValueError):
        # writing to closed aout stream raises ValueError
        log.exception("Unable to write RPC response")


class RpcMsgChannel(RpcChannel):
    def __init__(self, conn: RpcMsgConnection, *, name: str, loop: AbstractEventLoop):
        self._conn = conn
        self._proxy = RemoteRpcProxy(conn, loop=loop)
        self.name = name

    @property
    def proxy(self) -> RpcInterface:
        return self._proxy

    async def pump(self, impl: RpcInterface | None = None) -> None:
        """Listen for JSONRPC message on connection input until stream EOF.

        impl: implements the methods for RPC calls received on stream input
        """
        impl = impl or NoClient()
        try:
            async with TaskGroup() as response_tasks:
                try:
                    while (msg := await self._conn.read()) is not None:
                        if isinstance(msg.payload, Response):
                            self._proxy.got_response(msg.payload, msg.id)
                        elif msg.id is None:
                            await impl.notify(msg.payload)
                        else:
                            fix_id = msg.id if isinstance(msg.id, str) else None
                            tbd = await impl.request(msg.payload, fix_id)
                            coro = await_send_response(self._conn, tbd, msg.id)
                            response_tasks.create_task(coro)
                finally:
                    await impl.close_and_wait()
                    self._proxy.end_of_incomming_responses()
                    log.debug(f"{self.name} pump done reading responses")
        finally:
            log.debug(f"{self.name} pump closing connection")
            await self._conn.close_and_wait()


def json_rpc_channel(
    ain: MinimalReader, aout: MinimalWriter, *, name: str, loop: AbstractEventLoop
) -> RpcMsgChannel:
    stream = JsonRpcMsgStream(DuplexStream(ain, aout))
    return RpcMsgChannel(stream, name=name, loop=loop)


class RpcMsgFileLogger(RpcMsgConnection):
    def __init__(self, conn: RpcMsgConnection, log_file: TextIO):
        self._conn = conn
        self._log_file = log_file
        self._log(state='open')

    async def close_and_wait(self) -> None:
        self._log(state='closing')
        await self._conn.close_and_wait()
        self._log(state='closed')

    async def write(self, msg: JsonRpcMsg) -> None:
        await self._conn.write(msg)
        self._log(sent=msg.to_lsp_obj())

    async def read(self) -> JsonRpcMsg | None:
        msg = await self._conn.read()
        if msg is None:
            self._log(state='eof')
        else:
            self._log(got=msg.to_lsp_obj())
        return msg

    def _log(self, **kwags: LspAny) -> None:
            t = datetime.datetime.now(datetime.UTC).time().isoformat()
            line = json.dumps({'t': t, **kwags}, separators=(',', ':'))
            self._log_file.write(line)
            self._log_file.write("\n")
