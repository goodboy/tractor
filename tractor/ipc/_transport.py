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
typing.Protocol based generic msg API, implement this class to add
backends for tractor.ipc.Channel

'''
from __future__ import annotations
from typing import (
    runtime_checkable,
    Type,
    Protocol,
    # TypeVar,
    ClassVar,
    TYPE_CHECKING,
)
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
)
import struct

import trio
import msgspec
from tricycle import BufferedReceiveStream

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
    MsgType,
    types as msgtypes,
    pretty_struct,
)

if TYPE_CHECKING:
    from tractor._addr import Address

log = get_logger(__name__)


# (codec, transport)
MsgTransportKey = tuple[str, str]


# from tractor.msg.types import MsgType
# ?TODO? this should be our `Union[*msgtypes.__spec__]` alias now right..?
# => BLEH, except can't bc prots must inherit typevar or param-spec
#   vars..
# MsgType = TypeVar('MsgType')


@runtime_checkable
class MsgTransport(Protocol):
#
# class MsgTransport(Protocol[MsgType]):
# ^-TODO-^ consider using a generic def and indexing with our
# eventual msg definition/types?
# - https://docs.python.org/3/library/typing.html#typing.Protocol

    stream: trio.SocketStream
    drained: list[MsgType]

    address_type: ClassVar[Type[Address]]
    codec_key: ClassVar[str]

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

    @classmethod
    def key(cls) -> MsgTransportKey:
        return (
            cls.codec_key,
            cls.address_type.proto_key,
        )

    @property
    def laddr(self) -> Address:
        ...

    @property
    def raddr(self) -> Address:
        ...

    @property
    def maddr(self) -> str:
        ...

    @classmethod
    async def connect_to(
        cls,
        addr: Address,
        **kwargs
    ) -> MsgTransport:
        ...

    @classmethod
    def get_stream_addrs(
        cls,
        stream: trio.abc.Stream
    ) -> tuple[
        Address,  # local
        Address   # remote
    ]:
        '''
        Return the transport protocol's address pair for the local
        and remote-peer side.

        '''
        ...

    # TODO, such that all `.raddr`s for each `SocketStream` are
    # delivered?
    # -[ ] move `.open_listener()` here and internally track the
    #     listener set, per address?
    # def get_peers(
    #     self,
    # ) -> list[Address]:
    #     ...



class MsgpackTransport(MsgTransport):

    # TODO: better naming for this?
    # -[ ] check how libp2p does naming for such things?
    codec_key: str = 'msgpack'

    def __init__(
        self,
        stream: trio.abc.Stream,
        prefix_size: int = 4,

        # XXX optionally provided codec pair for `msgspec`:
        # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
        #
        # TODO: define this as a `Codec` struct which can be
        # overriden dynamically by the application/runtime?
        codec: MsgCodec = None,

    ) -> None:
        self.stream = stream
        (
            self._laddr,
            self._raddr,
        ) = self.get_stream_addrs(stream)

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

        tpt_name: str = f'{type(self).__name__!r}'
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
                        f'{tpt_name} already closed by peer\n'
                    ),
                    src_exc=trans_err,
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
            except trio.ClosedResourceError as cre:
                closure_err = cre

                raise TransportClosed(
                    message=(
                        f'{tpt_name} was already closed locally ?\n'
                    ),
                    src_exc=closure_err,
                    loglevel='error',
                    raise_on_report=(
                        'another task closed this fd' in closure_err.args
                    ),
                ) from closure_err

            # graceful TCP EOF disconnect
            if header == b'':
                raise TransportClosed(
                    message=(
                        f'{tpt_name} already gracefully closed\n'
                    ),
                    loglevel='transport',
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
        hide_tb: bool = True,

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
            try:
                return await self.stream.send_all(size + bytes_data)
            except (
                trio.BrokenResourceError,
                trio.ClosedResourceError,
            ) as _re:
                trans_err = _re
                tpt_name: str = f'{type(self).__name__!r}'

                match trans_err:

                    # XXX, specifc to UDS transport and its,
                    # well, "speediness".. XD
                    # |_ likely todo with races related to how fast
                    #    the socket is setup/torn-down on linux
                    #    as it pertains to rando pings from the
                    #    `.discovery` subsys and protos.
                    case trio.BrokenResourceError() if (
                        '[Errno 32] Broken pipe'
                        in
                        trans_err.args[0]
                    ):
                        tpt_closed = TransportClosed.from_src_exc(
                            message=(
                                f'{tpt_name} already closed by peer\n'
                            ),
                            body=f'{self}\n',
                            src_exc=trans_err,
                            raise_on_report=True,
                            loglevel='transport',
                        )
                        raise tpt_closed from trans_err

                    # case trio.ClosedResourceError() if (
                    #     'this socket was already closed'
                    #     in
                    #     trans_err.args[0]
                    # ):
                    #     tpt_closed = TransportClosed.from_src_exc(
                    #         message=(
                    #             f'{tpt_name} already closed by peer\n'
                    #         ),
                    #         body=f'{self}\n',
                    #         src_exc=trans_err,
                    #         raise_on_report=True,
                    #         loglevel='transport',
                    #     )
                    #     raise tpt_closed from trans_err

                    # unless the disconnect condition falls under "a
                    # normal operation breakage" we usualy console warn
                    # about it.
                    case _:
                        log.exception(
                            f'{tpt_name} layer failed pre-send ??\n'
                        )
                        raise trans_err

        # ?TODO? does it help ever to dynamically show this
        # frame?
        # try:
        #     <the-above_code>
        # except BaseException as _err:
        #     err = _err
        #     if not isinstance(err, MsgTypeError):
        #         __tracebackhide__: bool = False
        #     raise

    async def recv(self) -> msgtypes.MsgType:
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

    @property
    def laddr(self) -> Address:
        return self._laddr

    @property
    def raddr(self) -> Address:
        return self._raddr

    def pformat(self) -> str:
        return (
            f'<{type(self).__name__}(\n'
            f' |_peers: 1\n'
            f'   laddr: {self._laddr}\n'
            f'   raddr: {self._raddr}\n'
            # f'\n'
            f' |_task: {self._task}\n'
            f')>\n'
        )

    __repr__ = __str__ = pformat
