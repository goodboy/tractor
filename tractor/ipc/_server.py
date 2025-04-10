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
High-level "IPC server" encapsulation for all your
multi-transport-protcol needs!

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
)
from functools import partial
import inspect
from types import (
    ModuleType,
)
from typing import (
    Callable,
    TYPE_CHECKING,
)

import trio
from trio import (
    EventStatistics,
    Nursery,
    TaskStatus,
    SocketListener,
)

from ..msg import Struct
from ..trionics import maybe_open_nursery
from .. import (
    _state,
    log,
)
from .._addr import Address
from ._transport import MsgTransport
from ._uds import UDSAddress
from ._tcp import TCPAddress

if TYPE_CHECKING:
    from .._runtime import Actor


log = log.get_logger(__name__)


class IPCEndpoint(Struct):
    '''
    An instance of an IPC "bound" address where the lifetime of the
    "ability to accept connections" (from clients) and then handle
    those inbound sessions or sequences-of-packets is determined by
    a (maybe pair of) nurser(y/ies).

    '''
    addr: Address
    listen_tn: Nursery
    stream_handler_tn: Nursery|None = None

    # NOTE, normally filled in by calling `.start_listener()`
    _listener: SocketListener|None = None

    # ?TODO, mk stream_handler hook into this ep instance so that we
    # always keep track of all `SocketStream` instances per
    # listener/ep?
    peer_tpts: dict[
        UDSAddress|TCPAddress,  # peer addr
        MsgTransport,  # handle to encoded-msg transport stream
    ] = {}

    async def start_listener(self) -> SocketListener:
        tpt_mod: ModuleType = inspect.getmodule(self.addr)
        lstnr: SocketListener = await tpt_mod.start_listener(
            addr=self.addr,
        )

        # NOTE, for handling the resolved non-0 port for
        # TCP/UDP network sockets.
        if (
            (unwrapped := lstnr.socket.getsockname())
            !=
            self.addr.unwrap()
        ):
            self.addr=self.addr.from_addr(unwrapped)

        self._listener = lstnr
        return lstnr

    def close_listener(
        self,
    ) -> bool:
        tpt_mod: ModuleType = inspect.getmodule(self.addr)
        closer: Callable = getattr(
            tpt_mod,
            'close_listener',
            False,
        )
        # when no defined closing is implicit!
        if not closer:
            return True
        return closer(
            addr=self.addr,
            lstnr=self._listener,
        )


class IPCServer(Struct):
    _parent_tn: Nursery
    _stream_handler_tn: Nursery
    _endpoints: list[IPCEndpoint] = []

    # syncs for setup/teardown sequences
    _shutdown: trio.Event|None = None

    # TODO, maybe just make `._endpoints: list[IPCEndpoint]` and
    # provide dict-views onto it?
    # @property
    # def addrs2eps(self) -> dict[Address, IPCEndpoint]:
    #     ...

    @property
    def proto_keys(self) -> list[str]:
        return [
            ep.addr.proto_key
            for ep in self._endpoints
        ]

    # def cancel_server(self) -> bool:
    def cancel(
        self,

        # !TODO, suport just shutting down accepting new clients,
        # not existing ones!
        # only_listeners: str|None = None

    ) -> bool:
        '''
        Cancel this IPC transport server nursery thereby
        preventing any new inbound IPC connections establishing.

        '''
        if self._parent_tn:
            # TODO: obvi a different server type when we eventually
            # support some others XD
            log.runtime(
                f'Cancelling server(s) for\n'
                f'{self.proto_keys!r}\n'
            )
            self._parent_tn.cancel_scope.cancel()
            return True

        log.warning(
            'No IPC server started before cancelling ?'
        )
        return False

    async def wait_for_shutdown(
        self,
    ) -> bool:
        if self._shutdown is not None:
            await self._shutdown.wait()
        else:
            tpt_protos: list[str] = []
            ep: IPCEndpoint
            for ep in self._endpoints:
                tpt_protos.append(ep.addr.proto_key)

            log.warning(
                'Transport server(s) may have been cancelled before started?\n'
                f'protos: {tpt_protos!r}\n'
            )

    @property
    def addrs(self) -> list[Address]:
        return [ep.addr for ep in self._endpoints]

    @property
    def accept_addrs(self) -> list[str, str|int]:
        '''
        The `list` of `Address.unwrap()`-ed active IPC endpoint addrs.

        '''
        return [ep.addr.unwrap() for ep in self._endpoints]

    def epsdict(self) -> dict[
        Address,
        IPCEndpoint,
    ]:
        return {
            ep.addr: ep
            for ep in self._endpoints
        }

    def is_shutdown(self) -> bool:
        if (ev := self._shutdown) is None:
            return False

        return ev.is_set()

    def pformat(self) -> str:

        fmtstr: str = (
            f' |_endpoints: {self._endpoints}\n'
        )
        if self._shutdown is not None:
            shutdown_stats: EventStatistics = self._shutdown.statistics()
            fmtstr += (
                f'\n'
                f' |_shutdown: {shutdown_stats}\n'
            )

        return (
            f'<IPCServer(\n'
            f'{fmtstr}'
            f')>\n'
        )

    __repr__ = pformat

    # TODO? maybe allow shutting down a `.listen_on()`s worth of
    # listeners by cancelling the corresponding
    # `IPCEndpoint._listen_tn` only ?
    # -[ ] in theory you could use this to
    #     "boot-and-wait-for-reconnect" of all current and connecting
    #     peers?
    #  |_ would require that the stream-handler is intercepted so we
    #     can intercept every `MsgTransport` (stream) and track per
    #     `IPCEndpoint` likely?
    #
    # async def unlisten(
    #     self,
    #     listener: SocketListener,
    # ) -> bool:
    #     ...

    async def listen_on(
        self,
        *,
        actor: Actor,
        accept_addrs: list[tuple[str, int|str]]|None = None,
        stream_handler_nursery: Nursery|None = None,
    ) -> list[IPCEndpoint]:
        '''
        Start `SocketListeners` (i.e. bind and call `socket.listen()`)
        for all IPC-transport-protocol specific `Address`-types
        in `accept_addrs`.

        '''
        from .._addr import (
            default_lo_addrs,
            wrap_address,
        )
        if accept_addrs is None:
            accept_addrs = default_lo_addrs([
                _state._def_tpt_proto
            ])

        else:
            accept_addrs: list[Address] = [
                wrap_address(a) for a in accept_addrs
            ]

        if self._shutdown is None:
            self._shutdown = trio.Event()

        elif self.is_shutdown():
            raise RuntimeError(
                f'IPC server has already terminated ?\n'
                f'{self}\n'
            )

        log.info(
            f'Binding to endpoints for,\n'
            f'{accept_addrs}\n'
        )
        eps: list[IPCEndpoint] = await self._parent_tn.start(
            partial(
                _serve_ipc_eps,
                actor=actor,
                server=self,
                stream_handler_tn=stream_handler_nursery,
                listen_addrs=accept_addrs,
            )
        )
        log.info(
            f'Started IPC endpoints\n'
            f'{eps}\n'
        )

        self._endpoints.extend(eps)
        # XXX, just a little bit of sanity
        group_tn: Nursery|None = None
        ep: IPCEndpoint
        for ep in eps:
            if ep.addr not in self.addrs:
                breakpoint()

            if group_tn is None:
                group_tn = ep.listen_tn
            else:
                assert group_tn is ep.listen_tn

        return eps


async def _serve_ipc_eps(
    *,
    actor: Actor,
    server: IPCServer,
    stream_handler_tn: Nursery,
    listen_addrs: list[tuple[str, int|str]],

    task_status: TaskStatus[
        Nursery,
    ] = trio.TASK_STATUS_IGNORED,
) -> None:
    '''
    Start IPC transport server(s) for the actor, begin
    listening/accepting new `trio.SocketStream` connections
    from peer actors via a `SocketListener`.

    This will cause an actor to continue living (and thus
    blocking at the process/OS-thread level) until
    `.cancel_server()` is called.

    '''
    try:
        listen_tn: Nursery
        async with trio.open_nursery() as listen_tn:

            eps: list[IPCEndpoint] = []
            # XXX NOTE, required to call `serve_listeners()` below.
            # ?TODO, maybe just pass `list(eps.values()` tho?
            listeners: list[trio.abc.Listener] = []
            for addr in listen_addrs:
                ep = IPCEndpoint(
                    addr=addr,
                    listen_tn=listen_tn,
                    stream_handler_tn=stream_handler_tn,
                )
                try:
                    log.info(
                        f'Starting new endpoint listener\n'
                        f'{ep}\n'
                    )
                    listener: trio.abc.Listener = await ep.start_listener()
                    assert listener is ep._listener
                    # if actor.is_registry:
                    #     import pdbp; pdbp.set_trace()

                except OSError as oserr:
                    if (
                        '[Errno 98] Address already in use'
                        in
                        oserr.args#[0]
                    ):
                        log.exception(
                            f'Address already in use?\n'
                            f'{addr}\n'
                        )
                    raise

                listeners.append(listener)
                eps.append(ep)

            _listeners: list[SocketListener] = await listen_tn.start(
                partial(
                    trio.serve_listeners,
                    handler=actor._stream_handler,
                    listeners=listeners,

                    # NOTE: configured such that new
                    # connections will stay alive even if
                    # this server is cancelled!
                    handler_nursery=stream_handler_tn
                )
            )
            # TODO, wow make this message better! XD
            log.info(
                'Started server(s)\n'
                +
                '\n'.join([f'|_{addr}' for addr in listen_addrs])
            )

            log.info(
                f'Started IPC endpoints\n'
                f'{eps}\n'
            )
            task_status.started(
                eps,
            )

    finally:
        if eps:
            addr: Address
            ep: IPCEndpoint
            for addr, ep in server.epsdict().items():
                ep.close_listener()
                server._endpoints.remove(ep)

        # if actor.is_arbiter:
        #     import pdbp; pdbp.set_trace()

        # signal the server is "shutdown"/"terminated"
        # since no more active endpoints are active.
        if not server._endpoints:
            server._shutdown.set()

@acm
async def open_ipc_server(
    actor: Actor,
    parent_tn: Nursery|None = None,
    stream_handler_tn: Nursery|None = None,

) -> IPCServer:

    async with maybe_open_nursery(
        nursery=parent_tn,
    ) as rent_tn:
        ipc_server = IPCServer(
            _parent_tn=rent_tn,
            _stream_handler_tn=stream_handler_tn or rent_tn,
        )
        try:
            yield ipc_server

        # except BaseException as berr:
        #     log.exception(
        #         'IPC server crashed on exit ?'
        #     )
        #     raise berr

        finally:
            # ?TODO, maybe we can ensure the endpoints are torndown
            # (and thus their managed listeners) beforehand to ensure
            # super graceful RPC mechanics?
            #
            # -[ ] but aren't we doing that already per-`listen_tn`
            #      inside `_serve_ipc_eps()` above?
            #
            # if not ipc_server.is_shutdown():
            #     ipc_server.cancel()
            #     await ipc_server.wait_for_shutdown()
            # assert ipc_server.is_shutdown()
            pass

            # !XXX TODO! lol so classic, the below code is rekt!
            #
            # XXX here is a perfect example of suppressing errors with
            # `trio.Cancelled` as per our demonstrating example,
            # `test_trioisms::test_acm_embedded_nursery_propagates_enter_err
            #
            # with trio.CancelScope(shield=True):
            #     await ipc_server.wait_for_shutdown()
