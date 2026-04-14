'''
`open_root_actor(tpt_bind_addrs=...)` test suite.

Verify all three runtime code paths for explicit IPC-server
bind-address selection in `_root.py`:

1. Non-registrar, no explicit bind -> random addrs from registry proto
2. Registrar, no explicit bind -> binds to registry_addrs
3. Explicit bind given -> wraps via `wrap_address()` and uses them

'''
import pytest
import trio
import tractor
from tractor.discovery._addr import (
    wrap_address,
)
from tractor.discovery._multiaddr import mk_maddr
from tractor._testing.addr import get_rando_addr


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------
def _bound_bindspaces(
    actor: tractor.Actor,
) -> set[str]:
    '''
    Collect the set of bindspace strings from the actor's
    currently bound IPC-server accept addresses.

    '''
    return {
        wrap_address(a).bindspace
        for a in actor.accept_addrs
    }


def _bound_wrapped(
    actor: tractor.Actor,
) -> list:
    '''
    Return the actor's accept addrs as wrapped `Address` objects.

    '''
    return [
        wrap_address(a)
        for a in actor.accept_addrs
    ]


# ------------------------------------------------------------------
# 1) Registrar + explicit tpt_bind_addrs
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    'addr_combo',
    [
        'bind-eq-reg',
        'bind-subset-reg',
        'bind-disjoint-reg',
    ],
    ids=lambda v: v,
)
def test_registrar_root_tpt_bind_addrs(
    reg_addr: tuple,
    tpt_proto: str,
    debug_mode: bool,
    addr_combo: str,
):
    '''
    Registrar root-actor with explicit `tpt_bind_addrs`:
    bound set must include all registry + all bind addr bindspaces
    (merge behavior).

    '''
    reg_wrapped = wrap_address(reg_addr)

    if addr_combo == 'bind-eq-reg':
        bind_addrs = [reg_addr]
        # extra secondary reg addr for subset test
        extra_reg = []

    elif addr_combo == 'bind-subset-reg':
        second_reg = get_rando_addr(tpt_proto)
        bind_addrs = [reg_addr]
        extra_reg = [second_reg]

    elif addr_combo == 'bind-disjoint-reg':
        # port=0 on same host -> completely different addr
        rando = wrap_address(reg_addr).get_random(
            bindspace=reg_wrapped.bindspace,
        )
        bind_addrs = [rando.unwrap()]
        extra_reg = []

    all_reg = [reg_addr] + extra_reg

    async def _main():
        async with tractor.open_root_actor(
            registry_addrs=all_reg,
            tpt_bind_addrs=bind_addrs,
            debug_mode=debug_mode,
        ):
            actor = tractor.current_actor()
            assert actor.is_registrar

            bound = actor.accept_addrs
            bound_bs = _bound_bindspaces(actor)

            # all registry bindspaces must appear in bound set
            for ra in all_reg:
                assert wrap_address(ra).bindspace in bound_bs

            # all bind-addr bindspaces must appear
            for ba in bind_addrs:
                assert wrap_address(ba).bindspace in bound_bs

            # registry addr must appear verbatim in bound
            # (after wrapping both sides for comparison)
            bound_w = _bound_wrapped(actor)
            assert reg_wrapped in bound_w

            if addr_combo == 'bind-disjoint-reg':
                assert len(bound) >= 2

    trio.run(_main)


@pytest.mark.parametrize(
    'addr_combo',
    [
        'bind-same-bindspace',
        'bind-disjoint',
    ],
    ids=lambda v: v,
)
def test_non_registrar_root_tpt_bind_addrs(
    daemon,
    reg_addr: tuple,
    tpt_proto: str,
    debug_mode: bool,
    addr_combo: str,
):
    '''
    Non-registrar root with explicit `tpt_bind_addrs`:
    bound set must exactly match the requested bind addrs
    (no merge with registry).

    '''
    reg_wrapped = wrap_address(reg_addr)

    if addr_combo == 'bind-same-bindspace':
        # same bindspace as reg but port=0 so we get a random port
        rando = reg_wrapped.get_random(
            bindspace=reg_wrapped.bindspace,
        )
        bind_addrs = [rando.unwrap()]

    elif addr_combo == 'bind-disjoint':
        rando = reg_wrapped.get_random(
            bindspace=reg_wrapped.bindspace,
        )
        bind_addrs = [rando.unwrap()]

    async def _main():
        async with tractor.open_root_actor(
            registry_addrs=[reg_addr],
            tpt_bind_addrs=bind_addrs,
            debug_mode=debug_mode,
        ):
            actor = tractor.current_actor()
            assert not actor.is_registrar

            bound = actor.accept_addrs
            assert len(bound) == len(bind_addrs)

            # bindspaces must match
            bound_bs = _bound_bindspaces(actor)
            for ba in bind_addrs:
                assert wrap_address(ba).bindspace in bound_bs

            # TCP port=0 should resolve to a real port
            for uw_addr in bound:
                w = wrap_address(uw_addr)
                if w.proto_key == 'tcp':
                    _host, port = uw_addr
                    assert port > 0

    trio.run(_main)


# ------------------------------------------------------------------
# 3) Non-registrar, default random bind (baseline)
# ------------------------------------------------------------------
def test_non_registrar_default_random_bind(
    daemon,
    reg_addr: tuple,
    debug_mode: bool,
):
    '''
    Baseline: no `tpt_bind_addrs`, daemon running.
    Bound bindspace matches registry bindspace,
    but bound addr differs from reg_addr (random).

    '''
    reg_wrapped = wrap_address(reg_addr)

    async def _main():
        async with tractor.open_root_actor(
            registry_addrs=[reg_addr],
            debug_mode=debug_mode,
        ):
            actor = tractor.current_actor()
            assert not actor.is_registrar

            bound_bs = _bound_bindspaces(actor)
            assert reg_wrapped.bindspace in bound_bs

            # bound addr should differ from the registry addr
            # (the runtime picks a random port/path)
            bound_w = _bound_wrapped(actor)
            assert reg_wrapped not in bound_w

    trio.run(_main)


# ------------------------------------------------------------------
# 4) Multiaddr string input
# ------------------------------------------------------------------
def test_tpt_bind_addrs_as_maddr_str(
    reg_addr: tuple,
    debug_mode: bool,
):
    '''
    Pass multiaddr strings as `tpt_bind_addrs`.
    Runtime should parse and bind successfully.

    '''
    reg_wrapped = wrap_address(reg_addr)
    # build a port-0 / random maddr string for binding
    rando = reg_wrapped.get_random(
        bindspace=reg_wrapped.bindspace,
    )
    maddr_str: str = str(mk_maddr(rando))

    async def _main():
        async with tractor.open_root_actor(
            registry_addrs=[reg_addr],
            tpt_bind_addrs=[maddr_str],
            debug_mode=debug_mode,
        ):
            actor = tractor.current_actor()
            assert actor.is_registrar

            for uw_addr in actor.accept_addrs:
                w = wrap_address(uw_addr)
                if w.proto_key == 'tcp':
                    _host, port = uw_addr
                    assert port > 0

    trio.run(_main)


# ------------------------------------------------------------------
# 5) Registrar merge produces union of binds
# ------------------------------------------------------------------
def test_registrar_merge_binds_union(
    tpt_proto: str,
    debug_mode: bool,
):
    '''
    Registrar + disjoint bind addr: bound set must include
    both registry and explicit bind addresses.

    '''
    reg_addr = get_rando_addr(tpt_proto)
    reg_wrapped = wrap_address(reg_addr)

    rando = reg_wrapped.get_random(
        bindspace=reg_wrapped.bindspace,
    )
    bind_addrs = [rando.unwrap()]

    # NOTE: for UDS, `get_random()` produces the same
    # filename for the same pid+actor-state, so the
    # "disjoint" premise only holds when the addrs
    # actually differ (always true for TCP, may
    # collide for UDS).
    expect_disjoint: bool = (
        tuple(reg_addr) != rando.unwrap()
    )

    async def _main():
        async with tractor.open_root_actor(
            registry_addrs=[reg_addr],
            tpt_bind_addrs=bind_addrs,
            debug_mode=debug_mode,
        ):
            actor = tractor.current_actor()
            assert actor.is_registrar

            bound = actor.accept_addrs
            bound_w = _bound_wrapped(actor)

            if expect_disjoint:
                # must have at least 2 (registry + bind)
                assert len(bound) >= 2

            # registry addr must appear in bound set
            assert reg_wrapped in bound_w

    trio.run(_main)


# ------------------------------------------------------------------
# 6) open_nursery forwards tpt_bind_addrs
# ------------------------------------------------------------------
def test_open_nursery_forwards_tpt_bind_addrs(
    reg_addr: tuple,
    debug_mode: bool,
):
    '''
    `open_nursery(tpt_bind_addrs=...)` forwards through
    `**kwargs` to `open_root_actor()`.

    '''
    reg_wrapped = wrap_address(reg_addr)
    rando = reg_wrapped.get_random(
        bindspace=reg_wrapped.bindspace,
    )
    bind_addrs = [rando.unwrap()]

    async def _main():
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
            tpt_bind_addrs=bind_addrs,
            debug_mode=debug_mode,
        ):
            actor = tractor.current_actor()
            bound_bs = _bound_bindspaces(actor)

            for ba in bind_addrs:
                assert wrap_address(ba).bindspace in bound_bs

    trio.run(_main)
