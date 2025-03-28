# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Inter-process comms abstractions

"""
from __future__ import annotations
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
)
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
)
import platform
from pprint import pformat
import struct
import typing
from typing import (
    Any,
    Callable,
    runtime_checkable,
    Protocol,
    Type,
    TypeVar,
)

import msgspec
from tricycle import BufferedReceiveStream
import trio

from tractor.log import get_logger
from tractor._exceptions import (
    MsgTypeError,
    pack_from_raise,
    TransportClosed,
    _mk_send_mte,
    _mk_recv_mte,
)
from tractor.msg import (
    _ctxvar_MsgCodec,
    # _codec,  XXX see `self._codec` sanity/debug checks
    MsgCodec,
    types as msgtypes,
    pretty_struct,
)

log = get_logger(__name__)

_is_windows = platform.system() == 'Windows'


def get_stream_addrs(
    stream: trio.SocketStream
) -> tuple[
    tuple[str, int],  # local
    tuple[str, int],  # remote
]:
    '''
    Return the `trio` streaming transport prot's socket-addrs for
    both the local and remote sides as a pair.

    '''
    # rn, should both be IP sockets
    lsockname = stream.socket.getsockname()
    rsockname = stream.socket.getpeername()
    return (
        tuple(lsockname[:2]),
        tuple(rsockname[:2]),
    )


# from tractor.msg.types import MsgType
# ?TODO? this should be our `Union[*msgtypes.__spec__]` alias now right..?
# => BLEH, except can't bc prots must inherit typevar or param-spec
#   vars..
MsgType = TypeVar('MsgType')


# TODO: break up this mod into a subpkg so we can start adding new
# backends and move this type stuff into a dedicated file.. Bo
#
@runtime_checkable
class MsgTransport(Protocol[MsgType]):
#
# ^-TODO-^ consider using a generic def and indexing with our
# eventual msg definition/types?
# - https://docs.python.org/3/library/typing.html#typing.Protocol

    stream: trio.SocketStream
    drained: list[MsgType]

    def __init__(self, stream: trio.SocketStream) -> None:
        ...

    # XXX: should this instead be called `.sendall()`?
    async def send(self, msg: MsgType) -> None:
        ...

    async def recv(self) -> MsgType:
        ...

    def __aiter__(self) -> MsgType:
        ...

    def connected(self) -> bool:
        ...

    # defining this sync otherwise it causes a mypy error because it
    # can't figure out it's a generator i guess?..?
    def drain(self) -> AsyncIterator[dict]:
        ...

    @property
    def laddr(self) -> tuple[str, int]:
        ...

    @property
    def raddr(self) -> tuple[str, int]:
        ...


# TODO: typing oddity.. not sure why we have to inherit here, but it
# seems to be an issue with `get_msg_transport()` returning
# a `Type[Protocol]`; probably should make a `mypy` issue?
class MsgpackTCPStream(MsgTransport):
    '''
    A ``trio.SocketStream`` delivering ``msgpack`` formatted data
    using the ``msgspec`` codec lib.

    '''
    layer_key: int = 4
    name_key: str = 'tcp'

    # TODO: better naming for this?
    # -[ ] check how libp2p does naming for such things?
    codec_key: str = 'msgpack'

    def __init__(
        self,
        stream: trio.SocketStream,
        prefix_size: int = 4,

        # XXX optionally provided codec pair for `msgspec`:
        # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
        #
        # TODO: define this as a `Codec` struct which can be
        # overriden dynamically by the application/runtime?
        codec: tuple[
            Callable[[Any], Any]|None,  # coder
            Callable[[type, Any], Any]|None,  # decoder
        ]|None = None,

    ) -> None:

        self.stream = stream
        assert self.stream.socket

        # should both be IP sockets
        self._laddr, self._raddr = get_stream_addrs(stream)

        # create read loop instance
        self._aiter_pkts = self._iter_packets()
        self._send_lock = trio.StrictFIFOLock()

        # public i guess?
        self.drained: list[dict] = []

        self.recv_stream = BufferedReceiveStream(
            transport_stream=stream
        )
        self.prefix_size = prefix_size

        # allow for custom IPC msg interchange format
        # dynamic override Bo
        self._task = trio.lowlevel.current_task()

        # XXX for ctxvar debug only!
        # self._codec: MsgCodec = (
        #     codec
        #     or
        #     _codec._ctxvar_MsgCodec.get()
        # )

    async def _iter_packets(self) -> AsyncGenerator[dict, None]:
        '''
        Yield `bytes`-blob decoded packets from the underlying TCP
        stream using the current task's `MsgCodec`.

        This is a streaming routine implemented as an async generator
        func (which was the original design, but could be changed?)
        and is allocated by a `.__call__()` inside `.__init__()` where
        it is assigned to the `._aiter_pkts` attr.

        '''
        decodes_failed: int = 0

        while True:
            try:
                header: bytes = await self.recv_stream.receive_exactly(4)
            except (
                ValueError,
                ConnectionResetError,

                # not sure entirely why we need this but without it we
                # seem to be getting racy failures here on
                # arbiter/registry name subs..
                trio.BrokenResourceError,

            ) as trans_err:

                loglevel = 'transport'
                match trans_err:
                    # case (
                    #     ConnectionResetError()
                    # ):
                    #     loglevel = 'transport'

                    # peer actor (graceful??) TCP EOF but `tricycle`
                    # seems to raise a 0-bytes-read?
                    case ValueError() if (
                        'unclean EOF' in trans_err.args[0]
                    ):
                        pass

                    # peer actor (task) prolly shutdown quickly due
                    # to cancellation
                    case trio.BrokenResourceError() if (
                        'Connection reset by peer' in trans_err.args[0]
                    ):
                        pass

                    # unless the disconnect condition falls under "a
                    # normal operation breakage" we usualy console warn
                    # about it.
                    case _:
                        loglevel: str = 'warning'


                raise TransportClosed(
                    message=(
                        f'IPC transport already closed by peer\n'
                        f'x]> {type(trans_err)}\n'
                        f'  |_{self}\n'
                    ),
                    loglevel=loglevel,
                ) from trans_err

            # XXX definitely can happen if transport is closed
            # manually by another `trio.lowlevel.Task` in the
            # same actor; we use this in some simulated fault
            # testing for ex, but generally should never happen
            # under normal operation!
            #
            # NOTE: as such we always re-raise this error from the
            #       RPC msg loop!
            except trio.ClosedResourceError as closure_err:
                raise TransportClosed(
                    message=(
                        f'IPC transport already manually closed locally?\n'
                        f'x]> {type(closure_err)} \n'
                        f'  |_{self}\n'
                    ),
                    loglevel='error',
                    raise_on_report=(
                        closure_err.args[0] == 'another task closed this fd'
                        or
                        closure_err.args[0] in ['another task closed this fd']
                    ),
                ) from closure_err

            # graceful TCP EOF disconnect
            if header == b'':
                raise TransportClosed(
                    message=(
                        f'IPC transport already gracefully closed\n'
                        f']>\n'
                        f' |_{self}\n'
                    ),
                    loglevel='transport',
                    # cause=???  # handy or no?
                )

            size: int
            size, = struct.unpack("<I", header)

            log.transport(f'received header {size}')  # type: ignore
            msg_bytes: bytes = await self.recv_stream.receive_exactly(size)

            log.transport(f"received {msg_bytes}")  # type: ignore
            try:
                # NOTE: lookup the `trio.Task.context`'s var for
                # the current `MsgCodec`.
                codec: MsgCodec = _ctxvar_MsgCodec.get()

                # XXX for ctxvar debug only!
                # if self._codec.pld_spec != codec.pld_spec:
                #     assert (
                #         task := trio.lowlevel.current_task()
                #     ) is not self._task
                #     self._task = task
                #     self._codec = codec
                #     log.runtime(
                #         f'Using new codec in {self}.recv()\n'
                #         f'codec: {self._codec}\n\n'
                #         f'msg_bytes: {msg_bytes}\n'
                #     )
                yield codec.decode(msg_bytes)

            # XXX NOTE: since the below error derives from
            # `DecodeError` we need to catch is specially
            # and always raise such that spec violations
            # are never allowed to be caught silently!
            except msgspec.ValidationError as verr:
                msgtyperr: MsgTypeError = _mk_recv_mte(
                    msg=msg_bytes,
                    codec=codec,
                    src_validation_error=verr,
                )
                # XXX deliver up to `Channel.recv()` where
                # a re-raise and `Error`-pack can inject the far
                # end actor `.uid`.
                yield msgtyperr

            except (
                msgspec.DecodeError,
                UnicodeDecodeError,
            ):
                if decodes_failed < 4:
                    # ignore decoding errors for now and assume they have to
                    # do with a channel drop - hope that receiving from the
                    # channel will raise an expected error and bubble up.
                    try:
                        msg_str: str|bytes = msg_bytes.decode()
                    except UnicodeDecodeError:
                        msg_str = msg_bytes

                    log.exception(
                        'Failed to decode msg?\n'
                        f'{codec}\n\n'
                        'Rxed bytes from wire:\n\n'
                        f'{msg_str!r}\n'
                    )
                    decodes_failed += 1
                else:
                    raise

    async def send(
        self,
        msg: msgtypes.MsgType,

        strict_types: bool = True,
        hide_tb: bool = False,

    ) -> None:
        '''
        Send a msgpack encoded py-object-blob-as-msg over TCP.

        If `strict_types == True` then a `MsgTypeError` will be raised on any
        invalid msg type

        '''
        __tracebackhide__: bool = hide_tb

        # XXX see `trio._sync.AsyncContextManagerMixin` for details
        # on the `.acquire()`/`.release()` sequencing..
        async with self._send_lock:

            # NOTE: lookup the `trio.Task.context`'s var for
            # the current `MsgCodec`.
            codec: MsgCodec = _ctxvar_MsgCodec.get()

            # XXX for ctxvar debug only!
            # if self._codec.pld_spec != codec.pld_spec:
            #     self._codec = codec
            #     log.runtime(
            #         f'Using new codec in {self}.send()\n'
            #         f'codec: {self._codec}\n\n'
            #         f'msg: {msg}\n'
            #     )

            if type(msg) not in msgtypes.__msg_types__:
                if strict_types:
                    raise _mk_send_mte(
                        msg,
                        codec=codec,
                    )
                else:
                    log.warning(
                        'Sending non-`Msg`-spec msg?\n\n'
                        f'{msg}\n'
                    )

            try:
                bytes_data: bytes = codec.encode(msg)
            except TypeError as _err:
                typerr = _err
                msgtyperr: MsgTypeError = _mk_send_mte(
                    msg,
                    codec=codec,
                    message=(
                        f'IPC-msg-spec violation in\n\n'
                        f'{pretty_struct.Struct.pformat(msg)}'
                    ),
                    src_type_error=typerr,
                )
                raise msgtyperr from typerr

            # supposedly the fastest says,
            # https://stackoverflow.com/a/54027962
            size: bytes = struct.pack("<I", len(bytes_data))
            return await self.stream.send_all(size + bytes_data)

        # ?TODO? does it help ever to dynamically show this
        # frame?
        # try:
        #     <the-above_code>
        # except BaseException as _err:
        #     err = _err
        #     if not isinstance(err, MsgTypeError):
        #         __tracebackhide__: bool = False
        #     raise

    @property
    def laddr(self) -> tuple[str, int]:
        return self._laddr

    @property
    def raddr(self) -> tuple[str, int]:
        return self._raddr

    async def recv(self) -> Any:
        return await self._aiter_pkts.asend(None)

    async def drain(self) -> AsyncIterator[dict]:
        '''
        Drain the stream's remaining messages sent from
        the far end until the connection is closed by
        the peer.

        '''
        try:
            async for msg in self._iter_packets():
                self.drained.append(msg)
        except TransportClosed:
            for msg in self.drained:
                yield msg

    def __aiter__(self):
        return self._aiter_pkts

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1


def get_msg_transport(

    key: tuple[str, str],

) -> Type[MsgTransport]:

    return {
        ('msgpack', 'tcp'): MsgpackTCPStream,
    }[key]


class Channel:
    '''
    An inter-process channel for communication between (remote) actors.

    Wraps a ``MsgStream``: transport + encoding IPC connection.

    Currently we only support ``trio.SocketStream`` for transport
    (aka TCP) and the ``msgpack`` interchange format via the ``msgspec``
    codec libary.

    '''
    def __init__(

        self,
        destaddr: tuple[str, int]|None,

        msg_transport_type_key: tuple[str, str] = ('msgpack', 'tcp'),

        # TODO: optional reconnection support?
        # auto_reconnect: bool = False,
        # on_reconnect: typing.Callable[..., typing.Awaitable] = None,

    ) -> None:

        # self._recon_seq = on_reconnect
        # self._autorecon = auto_reconnect

        self._destaddr = destaddr
        self._transport_key = msg_transport_type_key

        # Either created in ``.connect()`` or passed in by
        # user in ``.from_stream()``.
        self._stream: trio.SocketStream|None = None
        self._transport: MsgTransport|None = None

        # set after handshake - always uid of far end
        self.uid: tuple[str, str]|None = None

        self._aiter_msgs = self._iter_msgs()
        self._exc: Exception|None = None  # set if far end actor errors
        self._closed: bool = False

        # flag set by ``Portal.cancel_actor()`` indicating remote
        # (possibly peer) cancellation of the far end actor
        # runtime.
        self._cancel_called: bool = False

    @property
    def msgstream(self) -> MsgTransport:
        log.info(
            '`Channel.msgstream` is an old name, use `._transport`'
        )
        return self._transport

    @property
    def transport(self) -> MsgTransport:
        return self._transport

    @classmethod
    def from_stream(
        cls,
        stream: trio.SocketStream,
        **kwargs,

    ) -> Channel:

        src, dst = get_stream_addrs(stream)
        chan = Channel(
            destaddr=dst,
            **kwargs,
        )

        # set immediately here from provided instance
        chan._stream: trio.SocketStream = stream
        chan.set_msg_transport(stream)
        return chan

    def set_msg_transport(
        self,
        stream: trio.SocketStream,
        type_key: tuple[str, str]|None = None,

        # XXX optionally provided codec pair for `msgspec`:
        # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
        codec: MsgCodec|None = None,

    ) -> MsgTransport:
        type_key = (
            type_key
            or
            self._transport_key
        )
        # get transport type, then
        self._transport = get_msg_transport(
            type_key
        # instantiate an instance of the msg-transport
        )(
            stream,
            codec=codec,
        )
        return self._transport

    @cm
    def apply_codec(
        self,
        codec: MsgCodec,

    ) -> None:
        '''
        Temporarily override the underlying IPC msg codec for
        dynamic enforcement of messaging schema.

        '''
        orig: MsgCodec = self._transport.codec
        try:
            self._transport.codec = codec
            yield
        finally:
            self._transport.codec = orig

    # TODO: do a .src/.dst: str for maddrs?
    def __repr__(self) -> str:
        if not self._transport:
            return '<Channel with inactive transport?>'

        return repr(
            self._transport.stream.socket._sock
        ).replace(  # type: ignore
            "socket.socket",
            "Channel",
        )

    @property
    def laddr(self) -> tuple[str, int]|None:
        return self._transport.laddr if self._transport else None

    @property
    def raddr(self) -> tuple[str, int]|None:
        return self._transport.raddr if self._transport else None

    async def connect(
        self,
        destaddr: tuple[Any, ...] | None = None,
        **kwargs

    ) -> MsgTransport:

        if self.connected():
            raise RuntimeError("channel is already connected?")

        destaddr = destaddr or self._destaddr
        assert isinstance(destaddr, tuple)

        stream = await trio.open_tcp_stream(
            *destaddr,
            **kwargs
        )
        transport = self.set_msg_transport(stream)

        log.transport(
            f'Opened channel[{type(transport)}]: {self.laddr} -> {self.raddr}'
        )
        return transport

    # TODO: something like,
    # `pdbp.hideframe_on(errors=[MsgTypeError])`
    # instead of the `try/except` hack we have rn..
    # seems like a pretty useful thing to have in general
    # along with being able to filter certain stack frame(s / sets)
    # possibly based on the current log-level?
    async def send(
        self,
        payload: Any,

        hide_tb: bool = False,

    ) -> None:
        '''
        Send a coded msg-blob over the transport.

        '''
        __tracebackhide__: bool = hide_tb
        try:
            log.transport(
                '=> send IPC msg:\n\n'
                f'{pformat(payload)}\n'
            )
            # assert self._transport  # but why typing?
            await self._transport.send(
                payload,
                hide_tb=hide_tb,
            )
        except BaseException as _err:
            err = _err  # bind for introspection
            if not isinstance(_err, MsgTypeError):
                # assert err
                __tracebackhide__: bool = False
            else:
                assert err.cid

            raise

    async def recv(self) -> Any:
        assert self._transport
        return await self._transport.recv()

        # TODO: auto-reconnect features like 0mq/nanomsg?
        # -[ ] implement it manually with nods to SC prot
        #      possibly on multiple transport backends?
        #  -> seems like that might be re-inventing scalability
        #     prots tho no?
        # try:
        #     return await self._transport.recv()
        # except trio.BrokenResourceError:
        #     if self._autorecon:
        #         await self._reconnect()
        #         return await self.recv()
        #     raise

    async def aclose(self) -> None:

        log.transport(
            f'Closing channel to {self.uid} '
            f'{self.laddr} -> {self.raddr}'
        )
        assert self._transport
        await self._transport.stream.aclose()
        self._closed = True

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.aclose(*args)

    def __aiter__(self):
        return self._aiter_msgs

    # ?TODO? run any reconnection sequence?
    # -[ ] prolly should be impl-ed as deco-API?
    #
    # async def _reconnect(self) -> None:
    #     """Handle connection failures by polling until a reconnect can be
    #     established.
    #     """
    #     down = False
    #     while True:
    #         try:
    #             with trio.move_on_after(3) as cancel_scope:
    #                 await self.connect()
    #             cancelled = cancel_scope.cancelled_caught
    #             if cancelled:
    #                 log.transport(
    #                     "Reconnect timed out after 3 seconds, retrying...")
    #                 continue
    #             else:
    #                 log.transport("Stream connection re-established!")

    #                 # on_recon = self._recon_seq
    #                 # if on_recon:
    #                 #     await on_recon(self)

    #                 break
    #         except (OSError, ConnectionRefusedError):
    #             if not down:
    #                 down = True
    #                 log.transport(
    #                     f"Connection to {self.raddr} went down, waiting"
    #                     " for re-establishment")
    #             await trio.sleep(1)

    async def _iter_msgs(
        self
    ) -> AsyncGenerator[Any, None]:
        '''
        Yield `MsgType` IPC msgs decoded and deliverd from
        an underlying `MsgTransport` protocol.

        This is a streaming routine alo implemented as an async-gen
        func (same a `MsgTransport._iter_pkts()`) gets allocated by
        a `.__call__()` inside `.__init__()` where it is assigned to
        the `._aiter_msgs` attr.

        '''
        assert self._transport
        while True:
            try:
                async for msg in self._transport:
                    match msg:
                        # NOTE: if transport/interchange delivers
                        # a type error, we pack it with the far
                        # end peer `Actor.uid` and relay the
                        # `Error`-msg upward to the `._rpc` stack
                        # for normal RAE handling.
                        case MsgTypeError():
                            yield pack_from_raise(
                                local_err=msg,
                                cid=msg.cid,

                                # XXX we pack it here bc lower
                                # layers have no notion of an
                                # actor-id ;)
                                src_uid=self.uid,
                            )
                        case _:
                            yield msg

            except trio.BrokenResourceError:

                # if not self._autorecon:
                raise

            await self.aclose()

            # if self._autorecon:  # attempt reconnect
            #     await self._reconnect()
            #     continue

    def connected(self) -> bool:
        return self._transport.connected() if self._transport else False


@acm
async def _connect_chan(
    host: str,
    port: int

) -> typing.AsyncGenerator[Channel, None]:
    '''
    Create and connect a channel with disconnect on context manager
    teardown.

    '''
    chan = Channel((host, port))
    await chan.connect()
    yield chan
    with trio.CancelScope(shield=True):
        await chan.aclose()
