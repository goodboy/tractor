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
)

import trio

from tractor.ipc._transport import MsgTransport
from tractor.ipc._types import (
    transport_from_addr,
    transport_from_stream,
)
from tractor._addr import (
    wrap_address,
    Address,
    AddressTypes
)
from tractor.log import get_logger
from tractor._exceptions import (
    MsgTypeError,
    pack_from_raise,
)
from tractor.msg import MsgCodec


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
        addr: AddressTypes,
        **kwargs
    ) -> Channel:
        addr: Address = wrap_address(addr)
        transport_cls = transport_from_addr(addr)
        transport = await transport_cls.connect_to(addr, **kwargs)

        log.transport(
            f'Opened channel[{type(transport)}]: {transport.laddr} -> {transport.raddr}'
        )
        return Channel(transport=transport)

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
            self._transport
        ).replace(  # type: ignore
            "socket.socket",
            "Channel",
        )

    @property
    def laddr(self) -> Address|None:
        return self._transport.laddr if self._transport else None

    @property
    def raddr(self) -> Address|None:
        return self._transport.raddr if self._transport else None

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
                try:
                    assert err.cid

                except KeyError:
                    raise err

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
    addr: AddressTypes
) -> typing.AsyncGenerator[Channel, None]:
    '''
    Create and connect a channel with disconnect on context manager
    teardown.

    '''
    chan = await Channel.from_addr(addr)
    yield chan
    with trio.CancelScope(shield=True):
        await chan.aclose()
