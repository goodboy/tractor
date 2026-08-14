'''
Unit tests for the `AF_TIPC` transport backend, `tractor.ipc._tipc`.

The kernel-touching cases are gated on `is_tipc_available()` since
the `tipc` module is NOT loaded by default (`sudo modprobe tipc`);
the pure address-algebra cases run everywhere.

'''
from __future__ import annotations
import errno
from socket import (
    SOCK_STREAM,
    SOL_SOCKET,
    SO_ACCEPTCONN,
)

import pytest
import trio

from tractor.ipc import _tipc
from tractor.ipc._tipc import (
    AF_TIPC,
    TIPC_ADDR_ID,
    TIPC_ADDR_NAME,
    TIPC_CLUSTER_SCOPE,
    TIPC_NODE_SCOPE,
    TIPC_ZONE_SCOPE,
    TRACTOR_STYPE,
    TIPCAddress,
    instance_from_seed,
    is_tipc_available,
    start_listener,
)


pytestmark = pytest.mark.tipc

requires_tipc = pytest.mark.skipif(
    not is_tipc_available(),
    reason=(
        '`tipc` kernel module not loaded (`sudo modprobe tipc`)'
    ),
)


# ------------------------------------------------------------------
# address algebra (no kernel needed)
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    'addr',
    [
        TIPCAddress.get_root(),
        TIPCAddress(
            _stype=TRACTOR_STYPE,
            _instance=42,
            _scope=TIPC_NODE_SCOPE,
        ),
    ],
    ids=['root', 'node-scoped'],
)
def test_addr_unwrap_roundtrip(addr: TIPCAddress):
    '''
    `.unwrap()` is proto-keyed and `.from_addr()` inverts it — for
    both the `tuple` form and the `list` form msgpack decodes to.

    '''
    unwrapped: tuple = addr.unwrap()
    assert unwrapped[0] == 'tipc' == TIPCAddress.proto_key
    assert len(unwrapped) == 4

    assert TIPCAddress.from_addr(unwrapped) == addr
    assert TIPCAddress.from_addr(list(unwrapped)) == addr


def test_addr_scope_defaults_when_omitted():
    '''
    A 3-elem `('tipc', stype, inst)` form defaults to the
    cluster-scope bindspace.

    '''
    addr: TIPCAddress = TIPCAddress.from_addr(
        ('tipc', TRACTOR_STYPE, 99),
    )
    assert addr._scope == TIPC_CLUSTER_SCOPE
    assert addr.bindspace == TIPCAddress.def_bindspace


def test_zone_scope_normalized_to_cluster():
    '''
    `TIPC_ZONE_SCOPE` is deprecated/aliased in modern kernels;
    accept it on input, fold it to cluster.

    '''
    addr: TIPCAddress = TIPCAddress.from_addr(
        ('tipc', TRACTOR_STYPE, 7, TIPC_ZONE_SCOPE),
    )
    assert addr._scope == TIPC_CLUSTER_SCOPE
    assert addr.is_valid


def test_addr_from_bare_port_id_raises():
    '''
    A `TIPC_ADDR_ID` 5-tuple carries no service-name so it can
    NEVER be wrapped; it must fail loudly rather than silently
    fabricate an un-dialable addr.

    This is the invariant that lets
    `TIPCAddress.rebind_from_sockname` be `False`.

    '''
    with pytest.raises(ValueError) as excinfo:
        TIPCAddress.from_addr((TIPC_ADDR_ID, 0, 12345, 0, 0))

    assert 'port-id' in str(excinfo.value)


def test_addr_is_valid_predicate():
    assert TIPCAddress.get_root().is_valid

    # instance 0 is not a bindable name
    assert not TIPCAddress(
        _stype=TRACTOR_STYPE,
        _instance=0,
    ).is_valid

    # service-types 0..63 are TIPC-internal (`TIPC_CFG_SRV`,
    # `TIPC_TOP_SRV`, ..)
    assert not TIPCAddress(
        _stype=1,
        _instance=1616,
    ).is_valid


def test_port_id_is_annotation_only():
    '''
    `.maybe_node`/`.maybe_ref` are *observed* metadata, excluded
    from `.unwrap()` exactly like `UDSAddress.maybe_pid`.

    '''
    addr: TIPCAddress = TIPCAddress.get_root()
    annotated: TIPCAddress = addr.with_port_id(
        node=0xdead,
        ref=1234,
    )
    assert annotated.unwrap() == addr.unwrap()
    assert annotated.maybe_ref == 1234
    assert '1234' in repr(annotated)


def test_instance_from_seed_is_pure():
    '''
    Same seed -> same instance (what the follow-up registrar-less
    discovery fast-path will lean on), and always clear of the
    reserved low range.

    '''
    for seed in ('doggy@123', 'kitty@456', ''):
        inst: int = instance_from_seed(seed)
        assert inst == instance_from_seed(seed)
        assert 64 <= inst < 2**32


def test_get_random_collision_resistance():
    '''
    A `.get_random()` clash does NOT raise `EADDRINUSE` — TIPC
    accepts multiple publishers of one name and round-robins
    connects between them, so a collision is *silent crosstalk*.

    Assert the 4-byte digest spreads well enough for that to stay
    improbable.

    NOTE the bound is birthday-statistical, not absolute:
    P(collision) ~= 1 - exp(-n**2 / 2**33) ~= 1.2e-2 for n=10k, so
    a strict `== n` assert would be ~1-in-86 flaky. P(>2
    collisions) is ~1e-7, hence the slack. See plan 01 §9 for the
    escalation path if this ever trips.

    '''
    n: int = 10_000
    addrs: list[TIPCAddress] = [
        TIPCAddress.get_random()
        for _ in range(n)
    ]
    instances: set[int] = {
        addr._instance
        for addr in addrs
    }
    assert len(instances) >= n - 2

    # every one is a legal, bindable name
    assert all(addr.is_valid for addr in addrs)


def test_get_random_honors_bindspace():
    addr: TIPCAddress = TIPCAddress.get_random(
        bindspace=TIPC_NODE_SCOPE,
    )
    assert addr.bindspace == TIPC_NODE_SCOPE == addr._scope


def test_eafnosupport_is_actionable_connerr(
    monkeypatch: pytest.MonkeyPatch,
):
    '''
    With no `tipc` module the kernel answers `EAFNOSUPPORT`; that
    MUST surface as a `ConnectionError` naming the fix rather than
    a bare `OSError`.

    '''
    class _NoTIPCKernel:
        @staticmethod
        def socket(*args, **kwargs):
            raise OSError(
                errno.EAFNOSUPPORT,
                'Address family not supported by protocol',
            )

    monkeypatch.setattr(_tipc, 'trio_socket', _NoTIPCKernel)

    async def main():
        await start_listener(addr=TIPCAddress.get_root())

    with pytest.raises(ConnectionError) as excinfo:
        trio.run(main)

    report: str = str(excinfo.value)
    assert 'modprobe tipc' in report
    assert type(excinfo.value.__cause__) is OSError


# ------------------------------------------------------------------
# kernel-touching
# ------------------------------------------------------------------

@requires_tipc
def test_listener_tolerates_so_acceptconn():
    '''
    `trio.SocketListener.__init__` asserts
    `getsockopt(SOL_SOCKET, SO_ACCEPTCONN)` is truthy, suppressing
    `OSError` for exotic families.

    Pin which of the two branches `AF_TIPC` actually takes (plan 01
    §3.1 left it as an assumption) so a kernel-side regression is
    caught here rather than as a mystery bind failure.

    '''
    async def main():
        addr: TIPCAddress = TIPCAddress.get_random()
        lstnr = await start_listener(addr=addr)
        try:
            assert lstnr.socket.getsockopt(
                SOL_SOCKET,
                SO_ACCEPTCONN,
            )
        finally:
            lstnr.socket.close()

    trio.run(main)


@requires_tipc
def test_bind_publishes_a_dialable_service_name():
    '''
    "Publishing a bind IS registration": `.bind()` a singleton
    name-seq and a second task resolves it by *name* — with NO
    `tractor` registrar in the loop.

    This is the core #378 property.

    '''
    async def main():
        addr: TIPCAddress = TIPCAddress.get_random()
        lstnr = await start_listener(addr=addr)

        accepted: list = []

        async def _accept():
            stream = await lstnr.accept()
            accepted.append(stream)
            await stream.send_all(b'woof')
            await stream.aclose()

        async with trio.open_nursery() as tn:
            tn.start_soon(_accept)
            await trio.sleep(0.05)

            sock = _tipc.trio_socket.socket(
                AF_TIPC,
                SOCK_STREAM,
            )
            # NOTE, connect by *name* -> the kernel does the
            # lookup, i.e. this call IS the discovery query.
            await sock.connect((
                TIPC_ADDR_NAME,
                addr._stype,
                addr._instance,
                0,  # domain: 0 == "anywhere in scope"
                addr._scope,
            ))
            stream = trio.SocketStream(sock)
            assert await stream.receive_some(16) == b'woof'
            await stream.aclose()

        assert len(accepted) == 1
        lstnr.socket.close()

    trio.run(main)


@requires_tipc
def test_getsockname_is_a_port_id_not_the_bound_name():
    '''
    The reason `TIPCAddress.rebind_from_sockname` is `False`.

    A NAMESEQ-bound listener's `getsockname()` answers a
    `TIPC_ADDR_ID` port-id, which never equals `.unwrap()` and
    cannot be wrapped back into a service name.

    '''
    async def main():
        addr: TIPCAddress = TIPCAddress.get_random()
        lstnr = await start_listener(addr=addr)
        try:
            sockname: tuple = lstnr.socket.getsockname()
            assert sockname[0] == TIPC_ADDR_ID
            assert sockname != addr.unwrap()
            with pytest.raises(ValueError):
                TIPCAddress.from_addr(sockname)
        finally:
            lstnr.socket.close()

    trio.run(main)


@requires_tipc
def test_duplicate_name_bind_does_not_raise():
    '''
    Unlike every other backend, TIPC permits *two* publishers of
    one service name and round-robins connects between them.

    Pin that observed behaviour — it's the whole reason
    `.get_random()` bothers with a well-spread digest, and a
    future kernel that starts raising `EADDRINUSE` here would be
    very good news worth noticing.

    '''
    async def main():
        addr: TIPCAddress = TIPCAddress.get_random()
        first = await start_listener(addr=addr)
        second = await start_listener(addr=addr)
        try:
            assert first.socket.getsockname() != second.socket.getsockname()
        finally:
            first.socket.close()
            second.socket.close()

    trio.run(main)
