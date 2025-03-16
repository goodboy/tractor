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
'''
TCP implementation of tractor.ipc._transport.MsgTransport protocol 

'''
from __future__ import annotations
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
)
import struct
from typing import (
    Any,
    Callable,
    Type,
)

import msgspec
from tricycle import BufferedReceiveStream
import trio

from tractor.log import get_logger
from tractor._exceptions import (
    MsgTypeError,
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
from tractor.ipc import MsgTransport


log = get_logger(__name__)


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
                        f'x)> {type(trans_err)}\n'
                        f' |_{self}\n'
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
                        f'x)> {type(closure_err)} \n'
                        f' |_{self}\n'
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
                        f')>\n'
                        f'|_{self}\n'
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
