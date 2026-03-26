'''
Stale-entry cleanup in the registrar's table.

'''
import subprocess

import tractor
import trio


async def unpack_reg(
    actor_or_portal: tractor.Portal|tractor.Actor,
):
    '''
    Get and unpack a "registry" RPC request from the registrar
    system.

    '''
    if getattr(actor_or_portal, 'get_registry', None):
        msg = await actor_or_portal.get_registry()
    else:
        msg = await actor_or_portal.run_from_ns('self', 'get_registry')

    return {
        tuple(key.split('.')): val
        for key, val in msg.items()
    }


@tractor.context
async def kill_transport(
    ctx: tractor.Context,
) -> None:

    await ctx.started()
    actor: tractor.Actor = tractor.current_actor()
    actor.ipc_server.cancel()
    await trio.sleep_forever()


# @pytest.mark.parametrize('use_signal', [False, True])
def test_stale_entry_is_deleted(
    debug_mode: bool,
    daemon: subprocess.Popen,
    start_method: str,
    reg_addr: tuple,
):
    '''
    Ensure that when a stale entry is detected in the registrar's
    table that the `find_actor()` API takes care of deleting the
    stale entry and not delivering a bad portal.

    '''
    async def main():

        name: str = 'transport_fails_actor'
        _reg_ptl: tractor.Portal
        an: tractor.ActorNursery
        async with (
            tractor.open_nursery(
                debug_mode=debug_mode,
                registry_addrs=[reg_addr],
            ) as an,
            tractor.get_registry(reg_addr) as _reg_ptl,
        ):
            ptl: tractor.Portal = await an.start_actor(
                name,
                enable_modules=[__name__],
            )
            async with ptl.open_context(
                kill_transport,
            ) as (first, ctx):
                async with tractor.find_actor(
                    name,
                    registry_addrs=[reg_addr],
                ) as maybe_portal:
                    # because the transitive
                    # `._discovery.maybe_open_portal()` call should
                    # fail and implicitly call `.delete_addr()`
                    assert maybe_portal is None
                    registry: dict = await unpack_reg(_reg_ptl)
                    assert ptl.chan.aid.uid not in registry

                # should fail since we knocked out the IPC tpt XD
                await ptl.cancel_actor()
                await an.cancel()

    trio.run(main)
