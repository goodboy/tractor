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
    _state,
    _addr,
)

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
    reg_addr: tuple,
    debug_mode: bool,
):
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

    trio.run(main)
