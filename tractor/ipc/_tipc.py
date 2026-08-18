# tractor: distributed structured concurrency.
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
`AF_TIPC` (Transparent Inter-Process Communication) implementation of
the `tractor.ipc._transport.MsgTransport` protocol.

TIPC is a linux-kernel cluster IPC protocol whose *service names* are
published in a cluster-wide name-table by the kernel itself. That
makes a `.bind()` literally a service **registration** and
a `.connect()` literally a service **lookup**, i.e. the discovery
machinery `tractor.discovery` normally implements with a registrar
actor comes for free, in-kernel.

An actor's TIPC address is therefore a *service name* pair,
`(stype, instance)`,

- a listener `.bind()`s the singleton published range
  `(stype, instance, instance)` as a `TIPC_ADDR_NAMESEQ`,
- a peer `.connect()`s that name as a `TIPC_ADDR_NAME` and the kernel
  resolves it,
- `TIPC_ADDR_ID` (a `(node, ref)` port-id) is only ever an *observed*
  address, never a user-facing one.

NOTE, the `tipc` kernel module is NOT loaded by default; see
`is_tipc_available()` and the `sudo modprobe tipc` hint carried in
this module's `ConnectionError` messages.

Normative refs are the kernel sources (the tipc.io docs are stale),
- `include/uapi/linux/tipc.h`
- `net/tipc/socket.c`

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
)
import errno
from hashlib import blake2b
import os
import socket
from socket import (
    SOCK_SEQPACKET,
    SOCK_STREAM,
)
import struct
import sys
from typing import (
    AsyncGenerator,
    Callable,
    ClassVar,
    Literal,
    Type,
    TYPE_CHECKING,
)
from uuid import uuid4

import msgspec
import trio
from trio import (
    socket as trio_socket,
    SocketListener,
)

from multiaddr import Multiaddr
from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor.discovery._multiaddr import mk_maddr
from tractor.ipc._transport import (
    MsgpackTransport,
)
from tractor.runtime._state import (
    current_actor,
    is_root_process,
)

if TYPE_CHECKING:
    from tractor.discovery._addr import TaggedTIPCAddress
    from tractor.runtime._runtime import Actor


log = get_logger()


# XXX, `AF_TIPC` and every `TIPC_*` constant are linux-ONLY in
# CPython's `socketmodule.c`. Mirror the `_uds.py` `SO_PASSCRED`
# precedent and fall back to the uapi values so this module stays
# **importable everywhere** — `.discovery._addr` builds its
# registration tables at import time (contract §2.3) — while
# `is_tipc_available()` remains the single *runtime* gate.
#
# values verified against `include/uapi/linux/tipc.h`
try:
    from socket import (
        AF_TIPC,
        SOL_TIPC,
        TIPC_ADDR_ID,
        TIPC_ADDR_NAME,
        TIPC_ADDR_NAMESEQ,
        TIPC_CLUSTER_SCOPE,
        TIPC_DEST_DROPPABLE,
        TIPC_HIGH_IMPORTANCE,
        TIPC_IMPORTANCE,
        TIPC_LOW_IMPORTANCE,
        TIPC_NODE_SCOPE,
        TIPC_PUBLISHED,
        TIPC_SUB_CANCEL,
        TIPC_SUB_PORTS,
        TIPC_SUB_SERVICE,
        TIPC_SUBSCR_TIMEOUT,
        TIPC_TOP_SRV,
        TIPC_WAIT_FOREVER,
        TIPC_WITHDRAWN,
        TIPC_ZONE_SCOPE,
    )
except ImportError:
    AF_TIPC: int = 30
    SOL_TIPC: int = 271
    TIPC_ADDR_NAMESEQ: int = 1
    TIPC_ADDR_NAME: int = 2
    TIPC_ADDR_ID: int = 3
    TIPC_ZONE_SCOPE: int = 1
    TIPC_CLUSTER_SCOPE: int = 2
    TIPC_NODE_SCOPE: int = 3
    TIPC_LOW_IMPORTANCE: int = 0
    TIPC_HIGH_IMPORTANCE: int = 2
    TIPC_IMPORTANCE: int = 127
    TIPC_DEST_DROPPABLE: int = 129
    TIPC_TOP_SRV: int = 1
    TIPC_SUB_PORTS: int = 1
    TIPC_SUB_SERVICE: int = 2
    TIPC_SUB_CANCEL: int = 4
    TIPC_PUBLISHED: int = 1
    TIPC_WITHDRAWN: int = 2
    TIPC_SUBSCR_TIMEOUT: int = 3
    TIPC_WAIT_FOREVER: int = -1


# `tractor`'s reserved TIPC service-class ("type"), spelling out
# ascii 'tr' in the high half and leaving the low 16b free for
# app-side partitioning via an explicit `TIPCAddress._stype`.
#
# NOTE, two `tractor` trees sharing BOTH a cluster and an `_stype`
# share a service-name space; see `.get_random()` on why that's
# only a *probabilistic* hazard.
TRACTOR_STYPE: int = 0x74_72_00_00

# TIPC reserves service-*types* 0..63 for its own internal services
# (`TIPC_CFG_SRV == 0`, `TIPC_TOP_SRV == 1`); see
# `include/uapi/linux/tipc.h`.
_tipc_reserved_stypes: range = range(0, 64)

# sentinel for "this addr was *observed* off a `TIPC_ADDR_ID`, so
# the peer's service-name is unknowable from the socket alone".
# See `MsgpackTIPCStream.get_stream_addrs()` and plan 01 §3.4.
TIPC_NAME_UNKNOWN: int = -1

# The topology event wire format carries no publication scope.
# Never promote caller context into observed address data.
TIPC_SCOPE_UNKNOWN: int = 0

# XXX, the kernel default (`TIPC_LOW_IMPORTANCE`), i.e. today this
# is a no-op knob preserving stock behaviour.
#
# ?TODO, TIPC can rank a connection's traffic under congestion —
# something no other backend can do — so the parent<->child
# *supervision* chan deserves `TIPC_HIGH_IMPORTANCE` while bulk app
# streams stay low. Wiring `_runtime.py`'s parent-chan path to pass
# it is deliberately a follow-up; see plan 01 §3.3 + §10.
TRACTOR_DEF_IMPORTANCE: int = TIPC_LOW_IMPORTANCE

_scope_names: dict[int, str] = {
    TIPC_SCOPE_UNKNOWN: 'unknown',
    TIPC_ZONE_SCOPE: 'zone',
    TIPC_CLUSTER_SCOPE: 'cluster',
    TIPC_NODE_SCOPE: 'node',
}

# see `is_tipc_available()`
_tipc_avail: bool|None = None


def is_tipc_available() -> bool:
    '''
    `True` iff this kernel can create an `AF_TIPC` socket, i.e. the
    `tipc` module is loaded (`sudo modprobe tipc`).

    Pure predicate; no side effects, no logging. The answer can't
    change without a `modprobe` so it's memoized after the first
    (one syscall) probe.

    '''
    global _tipc_avail
    if sys.platform != 'linux':
        _tipc_avail = False
        return _tipc_avail

    if _tipc_avail is None:
        try:
            socket.socket(
                AF_TIPC,
                SOCK_STREAM,
            ).close()
            _tipc_avail = True
        except OSError:
            _tipc_avail = False

    return _tipc_avail


class TIPCAddress(
    msgspec.Struct,
    frozen=True,
):
    '''
    A TIPC *service name* as an address, i.e. the
    `(type, instance)` pair a listener publishes and a peer
    resolves, plus the optionally-*observed* `TIPC_ADDR_ID`
    port-id of a live connection.

    '''
    _stype: int
    _instance: int
    _scope: int = TIPC_CLUSTER_SCOPE

    # observed-only, from a `TIPC_ADDR_ID` `getsockname()`/
    # `getpeername()`; excluded from `.unwrap()` exactly like
    # `UDSAddress.maybe_pid`.
    maybe_node: int|None = None
    maybe_ref: int|None = None

    proto_key: ClassVar[str] = 'tipc'
    unwrapped_type: ClassVar[type] = tuple
    def_bindspace: ClassVar[int] = TIPC_CLUSTER_SCOPE

    # XXX, TIPC's `getsockname()` answers a `TIPC_ADDR_ID` port-id
    # and NEVER the name-seq we bound, so the `Endpoint`-level
    # reconciliation would clobber a dialable service-name with an
    # un-dialable port-id. There's also nothing to learn: unlike
    # tcp's `port=0` there is no kernel-assigned-name analogue.
    rebind_from_sockname: ClassVar[bool] = False

    @property
    def bindspace(self) -> int:
        '''
        The TIPC *scope*, i.e. literally "the set of hosts from
        which this published name is reachable": `TIPC_NODE_SCOPE`
        for same-host-only (the UDS analogue),
        `TIPC_CLUSTER_SCOPE` for cluster-visible.

        '''
        return self._scope

    @property
    def is_valid(self) -> bool:
        '''
        Is this a *publishable/dialable* service name?

        NOTE the `> 0` (rather than `!= 0`) guards double-duty as
        the `TIPC_NAME_UNKNOWN` reject, i.e. an addr merely
        *observed* off a peer's port-id is never dialable.

        '''
        return (
            self._instance > 0
            and
            self._stype > 0
            and
            self._stype not in _tipc_reserved_stypes
            and
            self._scope in (
                TIPC_NODE_SCOPE,
                TIPC_CLUSTER_SCOPE,
            )
        )

    @classmethod
    def from_addr(
        cls,
        addr: tuple|list,
    ) -> TIPCAddress:
        match addr:
            # our proto-keyed unwrapped form, w/ scope optional
            case (
                ('tipc', int() as stype, int() as inst, int() as scope)
                |
                ['tipc', int() as stype, int() as inst, int() as scope]
            ):
                return TIPCAddress(
                    _stype=stype,
                    _instance=inst,
                    _scope=_norm_scope(scope),
                )

            case (
                ('tipc', int() as stype, int() as inst)
                |
                ['tipc', int() as stype, int() as inst]
            ):
                return TIPCAddress(
                    _stype=stype,
                    _instance=inst,
                )

            # a kernel-observed `TIPC_ADDR_ID` 5-tuple.
            #
            # XXX, a port-id carries NO service-name info, so we
            # cannot reconstruct `(stype, instance)` from it. This
            # is exactly why `.rebind_from_sockname` is `False`;
            # if you land here something re-enabled that path.
            case (int() as atype, *_) if atype == TIPC_ADDR_ID:
                raise ValueError(
                    f'Can not wrap a bare TIPC_ADDR_ID port-id !\n'
                    f'addr: {addr!r}\n'
                    f'\n'
                    f'A port-id carries no service-name, so the\n'
                    f'`(stype, instance)` identity is unrecoverable.\n'
                    f'Use `.with_port_id()` to *annotate* a known\n'
                    f'{cls.__name__} instead.\n'
                )

            case _:
                raise TypeError(
                    f'Bad unwrapped-address for {cls} !\n'
                    f'{addr!r}\n'
                )

    def unwrap(self) -> TaggedTIPCAddress:
        # NOTE, proto-keyed (w/ the `multiaddr` proto spelling) so
        # `wrap_address()` can dispatch unambiguously against the
        # other backends' 2-tuple forms; see contract §1.1.
        return (
            'tipc',
            self._stype,
            self._instance,
            self._scope,
        )

    def with_port_id(
        self,
        node: int|None,
        ref: int|None,
    ) -> TIPCAddress:
        '''
        A copy annotated with an *observed* `TIPC_ADDR_ID`
        port-id, purely for logging/`__repr__`.

        '''
        return msgspec.structs.replace(
            self,
            maybe_node=node,
            maybe_ref=ref,
        )

    @classmethod
    def get_random(
        cls,
        bindspace: int|None = None,
    ) -> TIPCAddress:
        '''
        A per-subactor ephemeral service-name.

        XXX, TIPC has NO kernel-assigned-instance analogue of tcp's
        `port=0`, so we must choose the instance ourselves — and a
        clash does **not** raise `EADDRINUSE`: TIPC happily accepts
        multiple publishers of one name and round-robins connects
        between them (verified). I.e. a collision manifests as
        *silent crosstalk*, not an error.

        So the instance is a `blake2b` digest of the actor UUID, or
        a per-call token outside a live runtime, giving a well-spread
        32b value. Being a pure fn of the seed it is also
        *reproducible*, which the (follow-up) registrar-less
        discovery fast-path wants.

        NOTE the residual risk is birthday-bounded: ~1.2e-2 for 10k
        names sharing one `_stype`. See plan 01 §9 for the
        escalation (post-bind verification) if that ever bites.

        '''
        pid: int = os.getpid()
        actor: Actor|None = current_actor(
            err_on_no_runtime=False,
        )
        if actor:
            seed: str = '.'.join(actor.aid.uid)
        else:
            if is_root_process():
                prefix: str = 'no_runtime_root'
            else:
                prefix: str = 'no_runtime_actor'

            # XXX, no live actor -> no `Aid` to key off, so mix
            # a per-CALL token in; w/o it the seed degenerates to
            # a pure fn of `(prefix, pid)` and two calls in one
            # proc alias to the SAME service name — the `_uds.py`
            # `.get_random()` hazard, but silent here.
            seed: str = f'{prefix}.{uuid4().hex[:8]}@{pid}'

        return TIPCAddress(
            _stype=TRACTOR_STYPE,
            _instance=instance_from_seed(seed),
            _scope=(
                bindspace
                if bindspace is not None
                else cls.def_bindspace
            ),
        )

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        '''
        Generic tpt-capability hook: `(ok, why_not)`.

        Consumed by the `tpt_protos` test fixture so a
        `--tpt-proto tipc` run on a box with no `tipc` module
        fails loudly and early rather than as a few hundred
        confusing connect timeouts. Apps can use it too.

        NOTE, deliberately spelled generically (NOT `is_tipc_*`)
        so the sibling env-dependent backends — `quic`/`iroh`
        (gh #353) and the `wg` netns bindspace (gh #482) — get
        the same gate for free.

        '''
        if is_tipc_available():
            return (True, '')

        return (
            False,
            'the `tipc` kernel module is not loaded'
            ' |_try: `sudo modprobe tipc`',
        )

    @classmethod
    def get_root(cls) -> TIPCAddress:
        # NOTE, `1616` mirrors `TCPAddress.get_root()`s port and
        # the UDS `registry@1616.sock` filename so the "1616 is
        # tractor's registrar" idiom holds across all backends.
        return TIPCAddress(
            _stype=TRACTOR_STYPE,
            _instance=1616,
            _scope=TIPC_CLUSTER_SCOPE,
        )

    def __repr__(self) -> str:
        if self._instance == TIPC_NAME_UNKNOWN:
            name: str = '<unknown-service>'
        else:
            name: str = f'0x{self._stype:08x}:{self._instance}'

        body: str = (
            f'{name}, {_scope_names.get(self._scope, self._scope)}'
        )
        if (node := self.maybe_node) is not None:
            body += f', @0x{node:08x}:{self.maybe_ref}'

        return (
            f'{type(self).__name__}'
            f'['
            f'{body}'
            f']'
        )


def instance_from_seed(seed: str) -> int:
    '''
    Derive a TIPC service *instance* from an actor-identity `seed`.

    A `blake2b` digest folded into `[64, 2**32)`; the low values are
    skipped to stay clear of TIPC's own reserved numbering
    conventions.

    '''
    inst: int = int.from_bytes(
        blake2b(
            seed.encode(),
            digest_size=4,
        ).digest(),
        'big',
    )
    return 64 + (inst % (2**32 - 64))


def _norm_scope(scope: int) -> int:
    '''
    Normalize a `TIPC_*_SCOPE` value.

    `TIPC_ZONE_SCOPE` is deprecated and aliased to cluster-scope by
    modern kernels; accept it on input and fold it.

    '''
    if scope == TIPC_ZONE_SCOPE:
        log.transport(
            f'Normalizing deprecated TIPC_ZONE_SCOPE -> cluster\n'
            f'scope: {scope!r}\n'
        )
        return TIPC_CLUSTER_SCOPE

    return scope


@cm
def _reraise_as_connerr(
    src_excs: tuple[Type[Exception]],
    addr: TIPCAddress,
):
    '''
    Normalize TIPC's `OSError`s into `ConnectionError`s.

    XXX REQUIRED, not polish: TIPC answers a lookup for an
    unpublished name with `EHOSTUNREACH` which python maps to a
    **bare** `OSError`, NOT a `ConnectionError` subtype (unlike
    `ECONNREFUSED` -> `ConnectionRefusedError`). Contract §4's
    discovery-ping path requires the `ConnectionError` shape.

    '''
    try:
        yield
    except src_excs as src_exc:
        match src_exc.errno:
            case errno.EAFNOSUPPORT:
                why: str = (
                    'TIPC unavailable — is the kernel module loaded?\n'
                    ' |_try: `sudo modprobe tipc`\n'
                )
            case errno.EHOSTUNREACH:
                why: str = (
                    'No TIPC publisher for this service name\n'
                    ' |_nothing has `.bind()`ed it in-scope\n'
                )
            case _:
                why: str = 'Bad TIPC service-name-as-address ??\n'

        raise ConnectionError(
            f'{why}'
            f'{addr}\n'
            f'\n'
            f'from src: {src_exc!r}\n'
        ) from src_exc


async def start_listener(
    addr: TIPCAddress,
    backlog: int = 128,
    **kwargs,
) -> SocketListener:
    '''
    Publish `addr` as a TIPC service name and listen on it.

    The `.bind()` of a singleton `TIPC_ADDR_NAMESEQ` range
    `(stype, instance, instance)` **is** the service registration —
    it's what shows up in `tipc nametable show` and what a peer's
    `.connect()`-by-name resolves against.

    NOTE, unlike every other backend a duplicate bind does NOT
    raise: TIPC permits multiple publishers of one name and
    round-robins connects between them. See
    `TIPCAddress.get_random()`.

    '''
    log.info(
        f'Attempting to publish TIPC service name\n'
        f'>[\n'
        f'|_{addr}\n'
    )
    with _reraise_as_connerr(
        src_excs=(OSError,),
        addr=addr,
    ):
        sock = trio_socket.socket(
            AF_TIPC,
            SOCK_STREAM,
        )
        await sock.bind((
            TIPC_ADDR_NAMESEQ,
            addr._stype,
            addr._instance,  # lower
            addr._instance,  # upper
            addr._scope,
        ))

    # NOTE, backlog matches `_uds.start_listener()`'s hard-won
    # value; a backlog of 1 overflows during concurrent
    # deregistration storms at actor-tree teardown.
    sock.listen(backlog)
    log.info(
        f'Published TIPC service name\n'
        f'[>\n'
        f' |_{addr}\n'
    )
    return SocketListener(sock)


# NOTE, deliberately NO `close_listener()`: there's no filesys
# entry to unlink and the kernel withdraws the published name on
# socket close. Per contract §1.2 absence means "closing is
# implicit".


@cm
def _close_on_error(sock):
    '''
    Close `sock` if the wrapped block raises.

    Equivalent to `trio._highlevel_open_unix_stream.close_on_error`
    but inlined so this (linux-cluster) backend doesn't import a
    *unix-domain* private module.

    '''
    try:
        yield sock
    except BaseException:
        sock.close()
        raise


class MsgpackTIPCStream(MsgpackTransport):
    '''
    A `trio.SocketStream` around an `AF_TIPC` service-name
    connection delivering `msgpack` encoded msgs via the `msgspec`
    codec lib.

    '''
    address_type = TIPCAddress
    layer_key: int = 4

    @property
    def maddr(self) -> Multiaddr|str:
        if not self.raddr:
            return '<unknown-peer>'

        return mk_maddr(self.raddr)

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1

    @classmethod
    async def connect_to(
        cls,
        destaddr: TIPCAddress,
        prefix_size: int = 4,
        codec: MsgCodec|None = None,
        importance: int = TRACTOR_DEF_IMPORTANCE,
        **kwargs,
    ) -> MsgpackTIPCStream:
        '''
        Dial `destaddr` **by service name**.

        NOTE, the `.connect()` here *is* the discovery lookup — the
        kernel resolves the published name-table entry for us, so
        there's no registrar hop on this path.

        '''
        sock = trio_socket.socket(
            AF_TIPC,
            SOCK_STREAM,
        )
        with _close_on_error(sock):
            sock.setsockopt(
                SOL_TIPC,
                TIPC_IMPORTANCE,
                importance,
            )
            # NOTE, surface undeliverable msgs as errors rather
            # than let the kernel silently drop them.
            sock.setsockopt(
                SOL_TIPC,
                TIPC_DEST_DROPPABLE,
                0,
            )
            with _reraise_as_connerr(
                src_excs=(OSError,),
                addr=destaddr,
            ):
                await sock.connect((
                    TIPC_ADDR_NAME,
                    destaddr._stype,
                    destaddr._instance,
                    0,  # domain: 0 == "anywhere in scope"
                    destaddr._scope,
                ))

            tpt_stream = MsgpackTIPCStream(
                trio.SocketStream(sock),
                prefix_size=prefix_size,
                codec=codec,
            )
            # XXX, the dialling side is the ONLY side that knows
            # the peer's *service name* (a port-id can't be
            # reversed into one), so re-assert it over the
            # observed-only `._raddr` derived above.
            #
            # Reuse that tolerant observation: a peer can withdraw
            # between `.connect()` and a second `.getpeername()`.
            observed_raddr: TIPCAddress = tpt_stream._raddr
            tpt_stream._raddr = destaddr.with_port_id(
                node=observed_raddr.maybe_node,
                ref=observed_raddr.maybe_ref,
            )
            return tpt_stream

    @classmethod
    def get_stream_addrs(
        cls,
        stream: trio.SocketStream,
    ) -> tuple[
        TIPCAddress,
        TIPCAddress,
    ]:
        '''
        Derive `(laddr, raddr)` from a connected TIPC socket.

        XXX, BOTH ends answer `TIPC_ADDR_ID` port-ids and a port-id
        carries NO service-name, so neither addr is dialable here;
        they're name-`TIPC_NAME_UNKNOWN` and carry only the
        observed `(node, ref)`.

        That's fine and deliberate (plan 01 §3.4a):

        - the *dialling* side overrides `._raddr` with the name it
          actually dialled (see `.connect_to()`),
        - the *accepting* side genuinely cannot know the peer's
          name from the socket — but it doesn't need to, since the
          `Aid` from `Channel._do_handshake()` already carries the
          peer's logical identity.

        '''
        sock = stream.socket
        return (
            _observed_addr(_maybe_sockaddr(sock.getsockname)),
            _observed_addr(_maybe_sockaddr(sock.getpeername)),
        )


def _maybe_sockaddr(
    getter: Callable[[], tuple],
) -> tuple|None:
    '''
    Call a `sock.getsockname`/`.getpeername` tolerantly.

    XXX REQUIRED for TIPC: unlike tcp/uds — where the kernel keeps
    answering the peer addr until *we* close — a TIPC socket whose
    peer has already gone answers `ENOTCONN`. That happens for any
    connect-then-immediately-drop peer: a port scan, a liveness
    probe, a cancelled dial.

    Since `MsgpackTransport.__init__()` calls `.get_stream_addrs()`
    (via `Channel.from_stream()`) BEFORE the handshake, letting the
    `OSError` fly would escape `handle_stream_from_peer()`s
    handshake tolerance (contract §4) and tear down the whole
    actor. A dead peer must cost us an addr, not the runtime.

    '''
    try:
        return getter()
    except OSError as oserr:
        log.transport(
            f'TIPC peer already gone, no port-id available\n'
            f'from src: {oserr!r}\n'
        )
        return None


def _port_id(
    sockaddr: tuple[int, int, int, int, int],
) -> tuple[int, int]:
    '''
    Unpack the `(node, ref)` of a `TIPC_ADDR_ID` 5-tuple as
    delivered by `getsockname()`/`getpeername()`.

    Layout is `(addrtype, node, ref, 0, scope)`; see
    `makesockaddr()`s `AF_TIPC` case in CPython's `socketmodule.c`.

    '''
    _, node, ref, *_ = sockaddr
    return (node, ref)


def _observed_addr(
    sockaddr: tuple[int, int, int, int, int]|None,
) -> TIPCAddress:
    '''
    Wrap a `TIPC_ADDR_ID` port-id as a name-less `TIPCAddress`
    usable for logging/`repr` only.

    A `None` `sockaddr` (peer already gone, see
    `_maybe_sockaddr()`) yields the same addr sans port-id.

    '''
    node: int|None = None
    ref: int|None = None
    if sockaddr is not None:
        node, ref = _port_id(sockaddr)

    return TIPCAddress(
        _stype=TIPC_NAME_UNKNOWN,
        _instance=TIPC_NAME_UNKNOWN,
        maybe_node=node,
        maybe_ref=ref,
    )


# ------------------------------------------------------------------
# layer B, the topology service (`TIPC_TOP_SRV`)
#
# The kernel will *push* us name-table `publish`/`withdraw` events,
# i.e. cluster-wide service (de)registration without a registrar
# actor and without polling. This is what makes #378's "end game
# cluster proto" claim real.
#
# Layouts below are from `include/uapi/linux/tipc.h` and were
# verified byte-for-byte against a live kernel (see the §5.2 probe
# notes in `ai/tpt-backends/01_tipc_backend.md`).
# ------------------------------------------------------------------

# struct tipc_subscr {
#   struct tipc_name_seq seq;   /* 3 * __u32: type, lower, upper */
#   __u32 timeout;
#   __u32 filter;
#   char  usr_handle[8];
# }
_SUBSCR_FMT: str = '=5I8s'

# struct tipc_event {
#   __u32 event, found_lower, found_upper;
#   struct tipc_portid port;    /* {__u32 ref; __u32 node;} */
#   struct tipc_subscr s;       /* the 28B subscription echo */
# }
#
# NOTE, 48B — NOT the 40 an earlier revision of the plan claimed.
_EVENT_FMT: str = '=10I8s'
_EVENT_SIZE: int = struct.calcsize(_EVENT_FMT)

# a `usr_handle[8]` tag so our subs are identifiable in
# `tipc nametable show`-adjacent debugging.
_SUBSCR_HANDLE: bytes = b'tractor\0'

_event_kinds: dict[int, str] = {
    TIPC_PUBLISHED: 'published',
    TIPC_WITHDRAWN: 'withdrawn',
    TIPC_SUBSCR_TIMEOUT: 'timeout',
}


class TIPCNameEvent(
    msgspec.Struct,
    frozen=True,
):
    '''
    A kernel name-table transition: some service name was
    published or withdrawn somewhere in the cluster.

    '''
    kind: Literal[
        'published',
        'withdrawn',
        'timeout',
    ]
    addr: TIPCAddress
    node: int
    ref: int

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}'
            f'['
            f'{self.kind}, {self.addr}, @0x{self.node:08x}:{self.ref}'
            f']'
        )


def _mk_subscr(
    stype: int,
    lower: int,
    upper: int,
    filt: int,
    timeout: int,
    handle: bytes = _SUBSCR_HANDLE,
) -> bytes:
    '''
    Pack a `struct tipc_subscr` for the topology server.

    NOTE, native (`'='`) byte-order is **accepted** by modern
    kernels — verified on a live box, both `publish` and
    `withdraw` events round-tripped w/ the subscription echoed
    back intact. An earlier revision of the plan proposed a
    `'>'`-retry endianness probe; it isn't needed.

    '''
    return struct.pack(
        _SUBSCR_FMT,
        stype,
        lower,
        upper,
        # XXX python exposes `TIPC_WAIT_FOREVER` as -1, so it MUST
        # be masked before packing into an unsigned field.
        timeout & 0xFFFF_FFFF,
        filt,
        handle,
    )


def _decode_name_event(
    raw: bytes,
    stype: int,
) -> TIPCNameEvent|None:
    '''
    Decode one `struct tipc_event`, or `None` if it's a runt/
    unrecognized frame.

    '''
    if len(raw) < _EVENT_SIZE:
        log.warning(
            f'Runt TIPC topology event, ignoring\n'
            f'len: {len(raw)} (want {_EVENT_SIZE})\n'
        )
        return None

    (
        event,
        found_lower,
        found_upper,
        ref,
        node,
        *_,  # the 28B subscription echo
    ) = struct.unpack(_EVENT_FMT, raw[:_EVENT_SIZE])

    if (kind := _event_kinds.get(event)) is None:
        log.warning(
            f'Unknown TIPC topology event code, ignoring\n'
            f'event: {event!r}\n'
        )
        return None

    return TIPCNameEvent(
        kind=kind,
        # NOTE, `tractor` only ever publishes *singleton* ranges
        # (`lower == upper`) so the lower bound IS the instance.
        #
        # XXX the event carries NO scope — do not fabricate one
        # from caller context and present it as observed data.
        addr=TIPCAddress(
            _stype=stype,
            _instance=found_lower,
            _scope=TIPC_SCOPE_UNKNOWN,
        ),
        node=node,
        ref=ref,
    )


async def _stream_name_events(
    sock,
    stype: int,
    tx: trio.MemorySendChannel,
) -> None:
    '''
    Read `struct tipc_event`s off a topology-server socket until
    it's closed, forwarding decoded ones to `tx`.

    '''
    try:
        while True:
            raw: bytes = await sock.recv(_EVENT_SIZE)
            if not raw:
                return

            if (ev := _decode_name_event(
                raw,
                stype=stype,
            )) is None:
                continue

            await tx.send(ev)
            if ev.kind == 'timeout':
                return

    except (
        trio.ClosedResourceError,
        trio.BrokenResourceError,
    ):
        # normal `@acm` teardown: the socket was closed under us
        return

    finally:
        tx.close()


@acm
async def open_topology_events(
    stype: int = TRACTOR_STYPE,
    lower: int = 0,
    upper: int = 0xFFFF_FFFF,
    filt: int = TIPC_SUB_SERVICE,
    timeout: int = TIPC_WAIT_FOREVER,
    buf_size: int = 64,
) -> AsyncGenerator[
    trio.MemoryReceiveChannel[TIPCNameEvent],
    None,
]:
    '''
    Subscribe to kernel name-table events for `stype` and yield a
    `trio` receive-channel of `TIPCNameEvent`.

    This is *push-based* service discovery: the kernel tells us
    when any actor in the cluster publishes or withdraws a name,
    so a registrar never has to poll `find_actor()`.

    `filt` selects the granularity,
    - `TIPC_SUB_SERVICE`: one event per *name* becoming
      (un)available — "does anyone serve this?"
    - `TIPC_SUB_PORTS`: one event per *publisher*, so N binders on
      one name give N events. Verified.

    NOTE this socket is `SOCK_SEQPACKET` and never goes through
    `MsgpackTransport` — the contract's "`SOCK_STREAM` only"
    constraint is about `MsgTransport` streams, not this.

    '''
    topsrv_addr = TIPCAddress(
        _stype=TIPC_TOP_SRV,
        _instance=TIPC_TOP_SRV,
        _scope=TIPC_CLUSTER_SCOPE,
    )
    sock = trio_socket.socket(
        AF_TIPC,
        SOCK_SEQPACKET,
    )
    with _close_on_error(sock):
        with _reraise_as_connerr(
            src_excs=(OSError,),
            addr=topsrv_addr,
        ):
            await sock.connect((
                TIPC_ADDR_NAME,
                TIPC_TOP_SRV,
                TIPC_TOP_SRV,
                0,  # domain: 0 == "anywhere in scope"
            ))
            await sock.send(
                _mk_subscr(
                    stype=stype,
                    lower=lower,
                    upper=upper,
                    filt=filt,
                    timeout=timeout,
                )
            )

    log.info(
        f'Subscribed to TIPC name-table events\n'
        f'[>\n'
        f' |_stype: 0x{stype:08x}\n'
        f' |_range: [{lower}, {upper}]\n'
        f' |_filter: {filt}\n'
    )
    tx: trio.MemorySendChannel
    rx: trio.MemoryReceiveChannel
    tx, rx = trio.open_memory_channel(buf_size)
    try:
        async with trio.open_nursery() as tn:
            tn.start_soon(
                _stream_name_events,
                sock,
                stype,
                tx,
            )
            try:
                yield rx
            finally:
                # XXX cancel BEFORE closing the fd!
                #
                # `.close()`ing out from under a pending
                # `.recv()` races: trio's retry can land on an
                # already-freed fd and raise a bare
                # `OSError(EBADF)` instead of the
                # `ClosedResourceError` the reader guards for —
                # which then escapes as an eg from the nursery.
                # Cancelling first makes teardown deterministic.
                tn.cancel_scope.cancel()
    finally:
        sock.close()
        await rx.aclose()
