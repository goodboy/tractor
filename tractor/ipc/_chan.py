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
from collections.abc import AsyncGenerator
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
)
import platform
from pprint import pformat
import typing
from typing import (
    Any,
    TYPE_CHECKING,
)
import warnings

import trio

from ._types import (
    transport_from_addr,
    transport_from_stream,
)
from tractor._addr import (
    is_wrapped_addr,
    wrap_address,
    Address,
    UnwrappedAddress,
)
from tractor.log import get_logger
from tractor._exceptions import (
    MsgTypeError,
    pack_from_raise,
    TransportClosed,
)
from tractor.msg import (
    Aid,
    MsgCodec,
)

if TYPE_CHECKING:
    from ._transport import MsgTransport


log = get_logger(__name__)

_is_windows = platform.system() == 'Windows'


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
        transport: MsgTransport|None = None,
        # TODO: optional reconnection support?
        # auto_reconnect: bool = False,
        # on_reconnect: typing.Callable[..., typing.Awaitable] = None,

    ) -> None:

        # self._recon_seq = on_reconnect
        # self._autorecon = auto_reconnect

        # Either created in ``.connect()`` or passed in by
        # user in ``.from_stream()``.
        self._transport: MsgTransport|None = transport

        # set after handshake - always info from peer end
        self.aid: Aid|None = None

        self._aiter_msgs = self._iter_msgs()
        self._exc: Exception|None = None
        # ^XXX! ONLY set if a remote actor sends an `Error`-msg
        self._closed: bool = False

        # flag set by ``Portal.cancel_actor()`` indicating remote
        # (possibly peer) cancellation of the far end actor
        # runtime.
        self._cancel_called: bool = False

    @property
    def uid(self) -> tuple[str, str]:
        '''
        Peer actor's unique id.

        '''
        msg: str = (
            f'`{type(self).__name__}.uid` is now deprecated.\n'
            'Use the new `.aid: tractor.msg.Aid` (struct) instead '
            'which also provides additional named (optional) fields '
            'beyond just the `.name` and `.uuid`.'
        )
        warnings.warn(
            msg,
            DeprecationWarning,
            stacklevel=2,
        )
        peer_aid: Aid = self.aid
        return (
            peer_aid.name,
            peer_aid.uuid,
        )

    @property
    def stream(self) -> trio.abc.Stream | None:
        return self._transport.stream if self._transport else None

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
        stream: trio.abc.Stream,
    ) -> Channel:
        transport_cls = transport_from_stream(stream)
        return Channel(
            transport=transport_cls(stream)
        )

    @classmethod
    async def from_addr(
        cls,
        addr: UnwrappedAddress,
        **kwargs
    ) -> Channel:

        if not is_wrapped_addr(addr):
            addr: Address = wrap_address(addr)

        transport_cls = transport_from_addr(addr)
        transport = await transport_cls.connect_to(
            addr,
            **kwargs,
        )
        assert transport.raddr == addr
        chan = Channel(transport=transport)

        # ?TODO, compact this into adapter level-methods?
        # -[ ] would avoid extra repr-calcs if level not active?
        #   |_ how would the `calc_if_level` look though? func?
        if log.at_least_level('runtime'):
            from tractor.devx import (
                pformat as _pformat,
            )
            chan_repr: str = _pformat.nest_from_op(
                input_op='[>',
                text=chan.pformat(),
                nest_indent=1,
            )
            log.runtime(
                f'Connected channel IPC transport\n'
                f'{chan_repr}'
            )
        return chan

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
    def pformat(
        self,
        privates: bool = False,
    ) -> str:
        if not self._transport:
            return '<Channel( with inactive transport? )>'

        tpt: MsgTransport = self._transport
        tpt_name: str = type(tpt).__name__
        tpt_status: str = (
            'connected' if self.connected()
            else 'closed'
        )
        repr_str: str = (
            f'<Channel(\n'
            f' |_status: {tpt_status!r}\n'
        ) + (
            f'   _closed={self._closed}\n'
            f'   _cancel_called={self._cancel_called}\n'
            if privates else ''
        ) + (  # peer-actor (processs) section
            f' |_peer: {self.aid.reprol()!r}\n'
            if self.aid else ' |_peer: <unknown>\n'
        ) + (
            f' |_msgstream: {tpt_name}\n'
            f'   maddr: {tpt.maddr!r}\n'
            f'   proto: {tpt.laddr.proto_key!r}\n'
            f'   layer: {tpt.layer_key!r}\n'
            f'   codec: {tpt.codec_key!r}\n'
            f'   .laddr={tpt.laddr}\n'
            f'   .raddr={tpt.raddr}\n'
        ) + (
            f'   ._transport.stream={tpt.stream}\n'
            f'   ._transport.drained={tpt.drained}\n'
            if privates else ''
        ) + (
            f'   _send_lock={tpt._send_lock.statistics()}\n'
            if privates else ''
        ) + (
            ')>\n'
        )
        return repr_str

    # NOTE: making this return a value that can be passed to
    # `eval()` is entirely **optional** FYI!
    # https://docs.python.org/3/library/functions.html#repr
    # https://docs.python.org/3/reference/datamodel.html#object.__repr__
    #
    # Currently we target **readability** from a (console)
    # logging perspective over `eval()`-ability since we do NOT
    # target serializing non-struct instances!
    # def __repr__(self) -> str:
    __str__ = pformat
    __repr__ = pformat

    @property
    def laddr(self) -> Address|None:
        return self._transport.laddr if self._transport else None

    @property
    def raddr(self) -> Address|None:
        return self._transport.raddr if self._transport else None

    @property
    def maddr(self) -> str:
        return self._transport.maddr if self._transport else '<no-tpt>'

    # TODO: something like,
    # `pdbp.hideframe_on(errors=[MsgTypeError])`
    # instead of the `try/except` hack we have rn..
    # seems like a pretty useful thing to have in general
    # along with being able to filter certain stack frame(s / sets)
    # possibly based on the current log-level?
    async def send(
        self,
        payload: Any,

        hide_tb: bool = True,

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
        except (
            BaseException,
            MsgTypeError,
            TransportClosed,
        )as _err:
            err = _err  # bind for introspection
            match err:
                case MsgTypeError():
                    try:
                        assert err.cid
                    except KeyError:
                        raise err
                case TransportClosed():
                    log.transport(
                        f'Transport stream closed due to\n'
                        f'{err.repr_src_exc()}\n'
                    )

                case _:
                    # never suppress non-tpt sources
                    __tracebackhide__: bool = False
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
            f'Closing channel to {self.aid} '
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

    async def _do_handshake(
        self,
        aid: Aid,

    ) -> Aid:
        '''
        Exchange `(name, UUIDs)` identifiers as the first
        communication step with any (peer) remote `Actor`.

        These are essentially the "mailbox addresses" found in
        "actor model" parlance.

        '''
        await self.send(aid)
        peer_aid: Aid = await self.recv()
        log.runtime(
            f'Received hanshake with peer '
            f'{peer_aid.reprol(sin_uuid=False)}\n'
        )
        # NOTE, we always are referencing the remote peer!
        self.aid = peer_aid
        return peer_aid


@acm
async def _connect_chan(
    addr: UnwrappedAddress
) -> typing.AsyncGenerator[Channel, None]:
    '''
    Create and connect a channel with disconnect on context manager
    teardown.

    '''
    chan = await Channel.from_addr(addr)
    yield chan
    with trio.CancelScope(shield=True):
        await chan.aclose()
