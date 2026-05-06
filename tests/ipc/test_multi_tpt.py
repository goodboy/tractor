'''
Verify the `enable_transports` param drives various
per-root/sub-actor IPC endpoint/server settings.

'''
from __future__ import annotations

import pytest
import trio
import tractor
from tractor import (
    Actor,
    Portal,
    ipc,
    msg,
)
from tractor.runtime import _state
from tractor.discovery import _addr

@tractor.context
async def chk_tpts(
    ctx: tractor.Context,
    tpt_proto_key: str,
):
    rtvars = _state._runtime_vars
    assert (
        tpt_proto_key
        in
        rtvars['_enable_tpts']
    )
    actor: Actor = tractor.current_actor()
    spec: msg.types.SpawnSpec = actor._spawn_spec
    assert spec._runtime_vars == rtvars

    # ensure individual IPC ep-addr types
    serv: ipc._server.Server = actor.ipc_server
    addr: ipc._types.Address
    for addr in serv.addrs:
        assert addr.proto_key == tpt_proto_key

    # Actor delegate-props enforcement
    assert (
        actor.accept_addrs
        ==
        serv.accept_addrs
    )

    await ctx.started(serv.accept_addrs)


# TODO, parametrize over mis-matched-proto-typed `registry_addrs`
# since i seems to work in `piker` but not exactly sure if both tcp
# & uds are being deployed then?
#
@pytest.mark.parametrize(
    'tpt_proto_key',
    ['tcp', 'uds'],
    ids=lambda item: f'ipc_tpt={item!r}'
)
def test_root_passes_tpt_to_sub(
    tpt_proto_key: str,
    tpt_proto: str,
    reg_addr: tuple,
    debug_mode: bool,
):
    # `reg_addr` is sourced from the CLI `--tpt-proto={tpt_proto}`,
    # so when the parametrized `tpt_proto_key` differs, the test
    # asks the runtime to `enable_transports=[<other_proto>]` while
    # pointing `registry_addrs` at a `reg_addr` of the wrong proto.
    # The layer-2 guard in `open_root_actor` is expected to fail
    # fast with `ValueError` on this mismatch (rather than the prior
    # silent hang during the registrar handshake).
    proto_mismatch: bool = (tpt_proto_key != tpt_proto)

    async def main():
        async with tractor.open_nursery(
            enable_transports=[tpt_proto_key],
            registry_addrs=[reg_addr],
            debug_mode=debug_mode,
        ) as an:

            assert (
                tpt_proto_key
                in
                _state._runtime_vars['_enable_tpts']
            )

            ptl: Portal = await an.start_actor(
                name='sub',
                enable_modules=[__name__],
            )
            async with ptl.open_context(
                chk_tpts,
                tpt_proto_key=tpt_proto_key,
            ) as (ctx, accept_addrs):

                uw_addr: tuple
                for uw_addr in accept_addrs:
                    addr = _addr.wrap_address(uw_addr)
                    assert addr.is_valid

            # shudown sub-actor(s)
            await an.cancel()

    if proto_mismatch:
        # mismatched proto must raise `ValueError` from the
        # `open_root_actor` runtime guard before any subactor spawn.
        with pytest.raises(ValueError) as excinfo:
            trio.run(main)
        msg: str = str(excinfo.value)
        assert 'enable_transports' in msg
        assert 'registry_addrs' in msg
        assert tpt_proto_key in msg or tpt_proto in msg
    else:
        trio.run(main)
