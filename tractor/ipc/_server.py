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
from collections import defaultdict
from contextlib import (
    asynccontextmanager as acm,
)
from functools import partial
from itertools import chain
import inspect
from pprint import pformat
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

# from ..devx import debug
from .._exceptions import (
    TransportClosed,
)
from .. import _rpc
from ..msg import (
    MsgType,
    Struct,
    types as msgtypes,
)
from ..trionics import maybe_open_nursery
from .. import (
    _state,
    log,
)
from .._addr import Address
from ._chan import Channel
from ._transport import MsgTransport
from ._uds import UDSAddress
from ._tcp import TCPAddress

if TYPE_CHECKING:
    from .._runtime import Actor
    from .._supervise import ActorNursery


log = log.get_logger(__name__)


async def maybe_wait_on_canced_subs(
    uid: tuple[str, str],
    chan: Channel,
    disconnected: bool,

    actor: Actor|None = None,
    chan_drain_timeout: float = 0.5,
    an_exit_timeout: float = 0.5,

) -> ActorNursery|None:
    '''
    When a process-local actor-nursery is found for the given actor
    `uid` (i.e. that peer is **also** a subactor of this parent), we
    attempt to (with timeouts) wait on,

    - all IPC msgs to drain on the (common) `Channel` such that all
      local `Context`-parent-tasks can also gracefully collect
      `ContextCancelled` msgs from their respective remote children
      vs. a `chan_drain_timeout`.

    - the actor-nursery to cancel-n-join all its supervised children
      (processes) *gracefully* vs. a `an_exit_timeout` and thus also
      detect cases where the IPC transport connection broke but
      a sub-process is detected as still alive (a case that happens
      when the subactor is still in an active debugger REPL session).

    If the timeout expires in either case we ofc report with warning.

    '''
    actor = actor or _state.current_actor()

    # XXX running outside actor-runtime usage,
    # - unit testing
    # - possibly manual usage (eventually) ?
    if not actor:
        return None

    local_nursery: (
        ActorNursery|None
    ) = actor._actoruid2nursery.get(uid)

    # This is set in `Portal.cancel_actor()`. So if
    # the peer was cancelled we try to wait for them
    # to tear down their side of the connection before
    # moving on with closing our own side.
    if (
        local_nursery
        and (
            actor._cancel_called
            or
            chan._cancel_called
        )
        #
        # ^-TODO-^ along with this is there another condition
        # that we should filter with to avoid entering this
        # waiting block needlessly?
        # -[ ] maybe `and local_nursery.cancelled` and/or
        #     only if the `._children` table is empty or has
        #     only `Portal`s with .chan._cancel_called ==
        #     True` as per what we had below; the MAIN DIFF
        #     BEING that just bc one `Portal.cancel_actor()`
        #     was called, doesn't mean the whole actor-nurse
        #     is gonna exit any time soon right!?
        #
        # or
        # all(chan._cancel_called for chan in chans)

    ):
        log.cancel(
            'Waiting on cancel request to peer..\n'
            f'c)=>\n'
            f'  |_{chan.uid}\n'
        )

        # XXX: this is a soft wait on the channel (and its
        # underlying transport protocol) to close from the
        # remote peer side since we presume that any channel
        # which is mapped to a sub-actor (i.e. it's managed
        # by local actor-nursery) has a message that is sent
        # to the peer likely by this actor (which may be in
        # a shutdown sequence due to cancellation) when the
        # local runtime here is now cancelled while
        # (presumably) in the middle of msg loop processing.
        chan_info: str = (
            f'{chan.uid}\n'
            f'|_{chan}\n'
            f'  |_{chan.transport}\n\n'
        )
        with trio.move_on_after(chan_drain_timeout) as drain_cs:
            drain_cs.shield = True

            # attempt to wait for the far end to close the
            # channel and bail after timeout (a 2-generals
            # problem on closure).
            assert chan.transport
            async for msg in chan.transport.drain():

                # try to deliver any lingering msgs
                # before we destroy the channel.
                # This accomplishes deterministic
                # ``Portal.cancel_actor()`` cancellation by
                # making sure any RPC response to that call is
                # delivered the local calling task.
                # TODO: factor this into a helper?
                log.warning(
                    'Draining msg from disconnected peer\n'
                    f'{chan_info}'
                    f'{pformat(msg)}\n'
                )
                # cid: str|None = msg.get('cid')
                cid: str|None = msg.cid
                if cid:
                    # deliver response to local caller/waiter
                    await actor._deliver_ctx_payload(
                        chan,
                        cid,
                        msg,
                    )
        if drain_cs.cancelled_caught:
            log.warning(
                'Timed out waiting on IPC transport channel to drain?\n'
                f'{chan_info}'
            )

        # XXX NOTE XXX when no explicit call to
        # `open_root_actor()` was made by the application
        # (normally we implicitly make that call inside
        # the first `.open_nursery()` in root-actor
        # user/app code), we can assume that either we
        # are NOT the root actor or are root but the
        # runtime was started manually. and thus DO have
        # to wait for the nursery-enterer to exit before
        # shutting down the local runtime to avoid
        # clobbering any ongoing subactor
        # teardown/debugging/graceful-cancel.
        #
        # see matching  note inside `._supervise.open_nursery()`
        #
        # TODO: should we have a separate cs + timeout
        # block here?
        if (
            # XXX SO either,
            #  - not root OR,
            #  - is root but `open_root_actor()` was
            #    entered manually (in which case we do
            #    the equiv wait there using the
            #    `devx.debug` sub-sys APIs).
            not local_nursery._implicit_runtime_started
        ):
            log.runtime(
                'Waiting on local actor nursery to exit..\n'
                f'|_{local_nursery}\n'
            )
            with trio.move_on_after(an_exit_timeout) as an_exit_cs:
                an_exit_cs.shield = True
                await local_nursery.exited.wait()

            # TODO: currently this is always triggering for every
            # sub-daemon spawned from the `piker.services._mngr`?
            # -[ ] how do we ensure that the IPC is supposed to
            #      be long lived and isn't just a register?
            # |_ in the register case how can we signal that the
            #    ephemeral msg loop was intentional?
            if (
                # not local_nursery._implicit_runtime_started
                # and
                an_exit_cs.cancelled_caught
            ):
                report: str = (
                    'Timed out waiting on local actor-nursery to exit?\n'
                    f'c)>\n'
                    f' |_{local_nursery}\n'
                )
                if children := local_nursery._children:
                    # indent from above local-nurse repr
                    report += (
                        f'   |_{pformat(children)}\n'
                    )

                log.warning(report)

        if disconnected:
            # if the transport died and this actor is still
            # registered within a local nursery, we report
            # that the IPC layer may have failed
            # unexpectedly since it may be the cause of
            # other downstream errors.
            entry: tuple|None = local_nursery._children.get(uid)
            if entry:
                proc: trio.Process
                _, proc, _ = entry

                if (
                    (poll := getattr(proc, 'poll', None))
                    and
                    poll() is None  # proc still alive
                ):
                    # TODO: change log level based on
                    # detecting whether chan was created for
                    # ephemeral `.register_actor()` request!
                    # -[ ] also, that should be avoidable by
                    #   re-using any existing chan from the
                    #   `._discovery.get_registry()` call as
                    #   well..
                    log.runtime(
                        f'Peer IPC broke but subproc is alive?\n\n'

                        f'<=x {chan.uid}@{chan.raddr}\n'
                        f'   |_{proc}\n'
                    )

    return local_nursery

# TODO multi-tpt support with per-proto peer tracking?
#
# -[x] maybe change to mod-func and rename for implied
#    multi-transport semantics?
# -[ ] register each stream/tpt/chan with the owning `IPCEndpoint`
#     so that we can query per tpt all peer contact infos?
#  |_[ ] possibly provide a global viewing via a
#        `collections.ChainMap`?
#
async def handle_stream_from_peer(
    stream: trio.SocketStream,

    *,
    server: IPCServer,

) -> None:
    '''
    Top-level `trio.abc.Stream` (i.e. normally `trio.SocketStream`)
    handler-callback as spawn-invoked by `trio.serve_listeners()`.

    Note that each call to this handler is as a spawned task inside
    any `IPCServer.listen_on()` passed `stream_handler_tn: Nursery`
    such that it is invoked as,

      IPCEndpoint.stream_handler_tn.start_soon(
          handle_stream,
          stream,
      )

    '''
    server._no_more_peers = trio.Event()  # unset by making new

    # TODO, debug_mode tooling for when hackin this lower layer?
    # with debug.maybe_open_crash_handler(
    #     pdb=True,
    # ) as boxerr:

    chan = Channel.from_stream(stream)
    con_status: str = (
        'New inbound IPC connection <=\n'
        f'|_{chan}\n'
    )

    # initial handshake with peer phase
    try:
        if actor := _state.current_actor():
            peer_aid: msgtypes.Aid = await chan._do_handshake(
                aid=actor.aid,
            )
    except (
        TransportClosed,
        # ^XXX NOTE, the above wraps `trio` exc types raised
        # during various `SocketStream.send/receive_xx()` calls
        # under different fault conditions such as,
        #
        # trio.BrokenResourceError,
        # trio.ClosedResourceError,
        #
        # Inside our `.ipc._transport` layer we absorb and
        # re-raise our own `TransportClosed` exc such that this
        # higher level runtime code can only worry one
        # "kinda-error" that we expect to tolerate during
        # discovery-sys related pings, queires, DoS etc.
    ):
        # XXX: This may propagate up from `Channel._aiter_recv()`
        # and `MsgpackStream._inter_packets()` on a read from the
        # stream particularly when the runtime is first starting up
        # inside `open_root_actor()` where there is a check for
        # a bound listener on the "arbiter" addr.  the reset will be
        # because the handshake was never meant took place.
        log.runtime(
            con_status
            +
            ' -> But failed to handshake? Ignoring..\n'
        )
        return

    uid: tuple[str, str] = (
        peer_aid.name,
        peer_aid.uuid,
    )
    # TODO, can we make this downstream peer tracking use the
    # `peer_aid` instead?
    familiar: str = 'new-peer'
    if _pre_chan := server._peers.get(uid):
        familiar: str = 'pre-existing-peer'
    uid_short: str = f'{uid[0]}[{uid[1][-6:]}]'
    con_status += (
        f' -> Handshake with {familiar} `{uid_short}` complete\n'
    )

    if _pre_chan:
        # con_status += (
        # ^TODO^ swap once we minimize conn duplication
        # -[ ] last thing might be reg/unreg runtime reqs?
        # log.warning(
        log.debug(
            f'?Wait?\n'
            f'We already have IPC with peer {uid_short!r}\n'
            f'|_{_pre_chan}\n'
        )

    # IPC connection tracking for both peers and new children:
    # - if this is a new channel to a locally spawned
    #   sub-actor there will be a spawn wait even registered
    #   by a call to `.wait_for_peer()`.
    # - if a peer is connecting no such event will exit.
    event: trio.Event|None = server._peer_connected.pop(
        uid,
        None,
    )
    if event:
        con_status += (
            ' -> Waking subactor spawn waiters: '
            f'{event.statistics().tasks_waiting}\n'
            f' -> Registered IPC chan for child actor {uid}@{chan.raddr}\n'
            # f'    {event}\n'
            # f'    |{event.statistics()}\n'
        )
        # wake tasks waiting on this IPC-transport "connect-back"
        event.set()

    else:
        con_status += (
            f' -> Registered IPC chan for peer actor {uid}@{chan.raddr}\n'
        )  # type: ignore

    chans: list[Channel] = server._peers[uid]
    # if chans:
    #     # TODO: re-use channels for new connections instead
    #     # of always new ones?
    #     # => will require changing all the discovery funcs..

    # append new channel
    # TODO: can we just use list-ref directly?
    chans.append(chan)

    con_status += ' -> Entering RPC msg loop..\n'
    log.runtime(con_status)

    # Begin channel management - respond to remote requests and
    # process received reponses.
    disconnected: bool = False
    last_msg: MsgType
    try:
        (
            disconnected,
            last_msg,
        ) = await _rpc.process_messages(
            chan=chan,
        )
    except trio.Cancelled:
        log.cancel(
            'IPC transport msg loop was cancelled\n'
            f'c)>\n'
            f' |_{chan}\n'
        )
        raise

    finally:

        # check if there are subs which we should gracefully join at
        # both the inter-actor-task and subprocess levels to
        # gracefully remote cancel and later disconnect (particularly
        # for permitting subs engaged in active debug-REPL sessions).
        local_nursery: ActorNursery|None = await maybe_wait_on_canced_subs(
            uid=uid,
            chan=chan,
            disconnected=disconnected,
        )

        # ``Channel`` teardown and closure sequence
        # drop ref to channel so it can be gc-ed and disconnected
        con_teardown_status: str = (
            f'IPC channel disconnected:\n'
            f'<=x uid: {chan.uid}\n'
            f'   |_{pformat(chan)}\n\n'
        )
        chans.remove(chan)

        # TODO: do we need to be this pedantic?
        if not chans:
            con_teardown_status += (
                f'-> No more channels with {chan.uid}'
            )
            server._peers.pop(uid, None)

        peers_str: str = ''
        for uid, chans in server._peers.items():
            peers_str += (
                f'uid: {uid}\n'
            )
            for i, chan in enumerate(chans):
                peers_str += (
                    f' |_[{i}] {pformat(chan)}\n'
                )

        con_teardown_status += (
            f'-> Remaining IPC {len(server._peers)} peers: {peers_str}\n'
        )

        # No more channels to other actors (at all) registered
        # as connected.
        if not server._peers:
            con_teardown_status += (
                'Signalling no more peer channel connections'
            )
            server._no_more_peers.set()

            # NOTE: block this actor from acquiring the
            # debugger-TTY-lock since we have no way to know if we
            # cancelled it and further there is no way to ensure the
            # lock will be released if acquired due to having no
            # more active IPC channels.
            if (
                _state.is_root_process()
                and
                _state.is_debug_mode()
            ):
                from ..devx import debug
                pdb_lock = debug.Lock
                pdb_lock._blocked.add(uid)

                # TODO: NEEEDS TO BE TESTED!
                # actually, no idea if this ever even enters.. XD
                #
                # XXX => YES IT DOES, when i was testing ctl-c
                # from broken debug TTY locking due to
                # msg-spec races on application using RunVar...
                if (
                    local_nursery
                    and
                    (ctx_in_debug := pdb_lock.ctx_in_debug)
                    and
                    (pdb_user_uid := ctx_in_debug.chan.uid)
                ):
                    entry: tuple|None = local_nursery._children.get(
                        tuple(pdb_user_uid)
                    )
                    if entry:
                        proc: trio.Process
                        _, proc, _ = entry

                        if (
                            (poll := getattr(proc, 'poll', None))
                            and poll() is None
                        ):
                            log.cancel(
                                'Root actor reports no-more-peers, BUT\n'
                                'a DISCONNECTED child still has the debug '
                                'lock!\n\n'
                                # f'root uid: {actor.uid}\n'
                                f'last disconnected child uid: {uid}\n'
                                f'locking child uid: {pdb_user_uid}\n'
                            )
                            await debug.maybe_wait_for_debugger(
                                child_in_debug=True
                            )

                # TODO: just bc a child's transport dropped
                # doesn't mean it's not still using the pdb
                # REPL! so,
                # -[ ] ideally we can check out child proc
                #  tree to ensure that its alive (and
                #  actually using the REPL) before we cancel
                #  it's lock acquire by doing the below!
                # -[ ] create a way to read the tree of each actor's
                #  grandchildren such that when an
                #  intermediary parent is cancelled but their
                #  child has locked the tty, the grandparent
                #  will not allow the parent to cancel or
                #  zombie reap the child! see open issue:
                #  - https://github.com/goodboy/tractor/issues/320
                # ------ - ------
                # if a now stale local task has the TTY lock still
                # we cancel it to allow servicing other requests for
                # the lock.
                if (
                    (db_cs := pdb_lock.get_locking_task_cs())
                    and not db_cs.cancel_called
                    and uid == pdb_user_uid
                ):
                    log.critical(
                        f'STALE DEBUG LOCK DETECTED FOR {uid}'
                    )
                    # TODO: figure out why this breaks tests..
                    db_cs.cancel()

        log.runtime(con_teardown_status)
    # finally block closure


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
    # level-triggered sig for whether "no peers are currently
    # connected"; field is **always** set to an instance but
    # initialized with `.is_set() == True`.
    _no_more_peers: trio.Event

    _endpoints: list[IPCEndpoint] = []

    # connection tracking & mgmt
    _peers: defaultdict[
        str,  # uaid
        list[Channel],  # IPC conns from peer
    ] = defaultdict(list)
    _peer_connected: dict[
        tuple[str, str],
        trio.Event,
    ] = {}

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

    def has_peers(
        self,
        check_chans: bool = False,
    ) -> bool:
        '''
        Predicate for "are there any active peer IPC `Channel`s at the moment?"

        '''
        has_peers: bool = not self._no_more_peers.is_set()
        if (
            has_peers
            and
            check_chans
        ):
            has_peers: bool = (
                any(chan.connected()
                    for chan in chain(
                        *self._peers.values()
                    )
                )
                and
                has_peers
            )

        return has_peers

    async def wait_for_no_more_peers(
        self,
        shield: bool = False,
    ) -> None:
        with trio.CancelScope(shield=shield):
            await self._no_more_peers.wait()

    async def wait_for_peer(
        self,
        uid: tuple[str, str],

    ) -> tuple[trio.Event, Channel]:
        '''
        Wait for a connection back from a (spawned sub-)actor with
        a `uid` using a `trio.Event`.

        Returns a pair of the event and the "last" registered IPC
        `Channel` for the peer with `uid`.

        '''
        log.debug(f'Waiting for peer {uid!r} to connect')
        event: trio.Event = self._peer_connected.setdefault(
            uid,
            trio.Event(),
        )
        await event.wait()
        log.debug(f'{uid!r} successfully connected back to us')
        mru_chan: Channel = self._peers[uid][-1]
        return (
            event,
            mru_chan,
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
        eps: list[IPCEndpoint] = self._endpoints

        state_repr: str = (
            f'{len(eps)!r} IPC-endpoints active'
        )
        fmtstr = (
            f' |_state: {state_repr}\n'
            f'   no_more_peers: {self.has_peers()}\n'
        )
        if self._shutdown is not None:
            shutdown_stats: EventStatistics = self._shutdown.statistics()
            fmtstr += (
                f'   task_waiting_on_shutdown: {shutdown_stats}\n'
            )

        fmtstr += (
            # TODO, use the `ppfmt()` helper from `modden`!
            f' |_endpoints: {pformat(self._endpoints)}\n'
            f' |_peers: {len(self._peers)} connected\n'
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

        log.runtime(
            f'Binding to endpoints for,\n'
            f'{accept_addrs}\n'
        )
        eps: list[IPCEndpoint] = await self._parent_tn.start(
            partial(
                _serve_ipc_eps,
                server=self,
                stream_handler_tn=stream_handler_nursery,
                listen_addrs=accept_addrs,
            )
        )
        log.runtime(
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
                    log.runtime(
                        f'Starting new endpoint listener\n'
                        f'{ep}\n'
                    )
                    listener: trio.abc.Listener = await ep.start_listener()
                    assert listener is ep._listener
                    # actor = _state.current_actor()
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
                    handler=partial(
                        handle_stream_from_peer,
                        server=server,
                    ),
                    listeners=listeners,

                    # NOTE: configured such that new
                    # connections will stay alive even if
                    # this server is cancelled!
                    handler_nursery=stream_handler_tn
                )
            )
            # TODO, wow make this message better! XD
            log.runtime(
                'Started server(s)\n'
                +
                '\n'.join([f'|_{addr}' for addr in listen_addrs])
            )

            log.runtime(
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

        # actor = _state.current_actor()
        # if actor.is_arbiter:
        #     import pdbp; pdbp.set_trace()

        # signal the server is "shutdown"/"terminated"
        # since no more active endpoints are active.
        if not server._endpoints:
            server._shutdown.set()

@acm
async def open_ipc_server(
    parent_tn: Nursery|None = None,
    stream_handler_tn: Nursery|None = None,

) -> IPCServer:

    async with maybe_open_nursery(
        nursery=parent_tn,
    ) as rent_tn:
        no_more_peers = trio.Event()
        no_more_peers.set()

        ipc_server = IPCServer(
            _parent_tn=rent_tn,
            _stream_handler_tn=stream_handler_tn or rent_tn,
            _no_more_peers=no_more_peers,
        )
        try:
            yield ipc_server
            log.runtime(
                f'Waiting on server to shutdown or be cancelled..\n'
                f'{ipc_server}'
            )
            # TODO? when if ever would we want/need this?
            # with trio.CancelScope(shield=True):
            #     await ipc_server.wait_for_shutdown()

        except BaseException as berr:
            log.exception(
                'IPC server caller crashed ??'
            )
            # ?TODO, maybe we can ensure the endpoints are torndown
            # (and thus their managed listeners) beforehand to ensure
            # super graceful RPC mechanics?
            #
            # -[ ] but aren't we doing that already per-`listen_tn`
            #      inside `_serve_ipc_eps()` above?
            #
            # ipc_server.cancel()
            raise berr
