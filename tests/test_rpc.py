'''
RPC (or maybe better labelled as "RTS: remote task scheduling"?)
related API and error checks.

'''
import itertools

import pytest
import tractor
import trio


async def sleep_back_actor(
    actor_name,
    func_name,
    func_defined,
    exposed_mods,
    *,
    reg_addr: tuple,
):
    if actor_name:
        async with tractor.find_actor(
            actor_name,
            # NOTE: must be set manually since
            # the subactor doesn't have the reg_addr
            # fixture code run in it!
            # TODO: maybe we should just set this once in the
            # _state mod and derive to all children?
            registry_addrs=[reg_addr],
        ) as portal:
            try:
                await portal.run(__name__, func_name)
            except tractor.RemoteActorError as err:
                if not func_defined:
                    expect = AttributeError
                if not exposed_mods:
                    expect = tractor.ModuleNotExposed

                assert err.boxed_type is expect
                raise
    else:
        await trio.sleep(float('inf'))


async def short_sleep():
    await trio.sleep(0)


@pytest.mark.parametrize(
    'to_call', [
        ([], 'short_sleep', tractor.RemoteActorError),
        ([__name__], 'short_sleep', tractor.RemoteActorError),
        ([__name__], 'fake_func', tractor.RemoteActorError),
        (['tmp_mod'], 'import doggy', ModuleNotFoundError),
        (['tmp_mod'], '4doggy', SyntaxError),
    ],
    ids=[
        'no_mods',
        'this_mod',
        'this_mod_bad_func',
        'fail_to_import',
        'fail_on_syntax',
    ],
)
def test_rpc_errors(
    reg_addr,
    to_call,
    testdir,
):
    '''
    Test errors when making various RPC requests to an actor
    that either doesn't have the requested module exposed or doesn't define
    the named function.

    '''
    exposed_mods, funcname, inside_err = to_call
    subactor_exposed_mods = []
    func_defined = globals().get(funcname, False)
    subactor_requests_to = 'root'
    remote_err = tractor.RemoteActorError

    # remote module that fails at import time
    if exposed_mods == ['tmp_mod']:
        # create an importable module with a bad import
        testdir.syspathinsert()
        # module should raise a ModuleNotFoundError at import
        testdir.makefile('.py', tmp_mod=funcname)

        # no need to expose module to the subactor
        subactor_exposed_mods = exposed_mods
        exposed_mods = []
        func_defined = False
        # subactor should not try to invoke anything
        subactor_requests_to = None
        # the module will be attempted to be imported locally but will
        # fail in the initial local instance of the actor
        remote_err = inside_err

    async def main():

        # spawn a subactor which calls us back
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
            enable_modules=exposed_mods.copy(),

            # NOTE: will halt test in REPL if uncommented, so only
            # do that if actually debugging subactor but keep it
            # disabled for the test.
            # debug_mode=True,
        ) as n:

            actor = tractor.current_actor()
            assert actor.is_arbiter
            await n.run_in_actor(
                sleep_back_actor,
                actor_name=subactor_requests_to,

                name='subactor',

                # function from the local exposed module space
                # the subactor will invoke when it RPCs back to this actor
                func_name=funcname,
                exposed_mods=exposed_mods,
                func_defined=True if func_defined else False,
                enable_modules=subactor_exposed_mods,
                reg_addr=reg_addr,
            )

    def run():
        trio.run(main)

    # handle both parameterized cases
    if exposed_mods and func_defined:
        run()
    else:
        # underlying errors aren't propagated upwards (yet)
        with pytest.raises(
            expected_exception=(remote_err, ExceptionGroup),
        ) as err:
            run()

        # get raw instance from pytest wrapper
        value = err.value

        # might get multiple `trio.Cancelled`s as well inside an inception
        if isinstance(value, ExceptionGroup):
            value = next(itertools.dropwhile(
                lambda exc: not isinstance(exc, tractor.RemoteActorError),
                value.exceptions
            ))

        if getattr(value, 'type', None):
            assert value.boxed_type is inside_err
