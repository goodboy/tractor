"""
RPC related
"""
import pytest
import tractor
import trio


async def sleep_back_actor(
    actor_name,
    func_name,
    func_defined,
    exposed_mods,
):
    async with tractor.find_actor(actor_name) as portal:
        try:
            await portal.run(__name__, func_name)
        except tractor.RemoteActorError as err:
            if not func_defined:
                expect = AttributeError
            if not exposed_mods:
                expect = tractor.ModuleNotExposed

            assert err.type is expect
            raise


async def short_sleep():
    await trio.sleep(0)


@pytest.mark.parametrize(
    'to_call', [
        ([], 'short_sleep'),
        ([__name__], 'short_sleep'),
        ([__name__], 'fake_func'),
    ],
    ids=['no_mods', 'this_mod', 'this_mod_bad_func'],
)
def test_rpc_errors(arb_addr, to_call):
    """Test errors when making various RPC requests to an actor
    that either doesn't have the requested module exposed or doesn't define
    the named function.
    """
    exposed_mods, funcname = to_call
    func_defined = globals().get(funcname, False)

    async def main():
        actor = tractor.current_actor()
        assert actor.is_arbiter

        # spawn a subactor which calls us back
        async with tractor.open_nursery() as n:
            await n.run_in_actor(
                'subactor',
                sleep_back_actor,
                actor_name=actor.name,
                # function from this module the subactor will invoke
                # when it RPCs back to this actor
                func_name=funcname,
                exposed_mods=exposed_mods,
                func_defined=True if func_defined else False,
            )

    def run():
        tractor.run(
            main,
            arbiter_addr=arb_addr,
            rpc_module_paths=exposed_mods,
        )

    # handle both parameterized cases
    if exposed_mods and func_defined:
        run()
    else:
        # underlying errors are propogated upwards (yet)
        with pytest.raises(tractor.RemoteActorError) as err:
            run()

        assert err.value.type is tractor.RemoteActorError
