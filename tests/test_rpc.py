'''
RPC (or maybe better labelled as "RTS: remote task scheduling"?)
related API and error checks.

'''
import itertools
from functools import partial
from unittest.mock import (
    AsyncMock,
    Mock,
)

import pytest
import tractor
import trio

from tractor._exceptions import TransportClosed
from tractor.runtime import _rpc


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


def test_rpc_runs_after_startack_disconnect():
    '''
    Complete an accepted RPC when its caller closes before `StartAck`.

    Registrar teardown opens short-lived `unregister_actor` RPCs. A
    loaded caller can close its channel while the registrar sends the
    acknowledgement; normalized `TransportClosed` previously escaped
    into the shared service nursery before the already-created
    coroutine was awaited. This fake fails the first response send and
    proves the RPC side effect still runs with no later send attempt.

    '''
    async def main():
        rpc_ran = trio.Event()

        chan = Mock()
        chan.send = AsyncMock(
            side_effect=TransportClosed(
                message='caller closed before StartAck',
            ),
        )
        chan.connected.return_value = False
        ctx = Mock(
            chan=chan,
            cid='rpc-cid',
            _scope=None,
            _task='rpc-task',
        )
        actor = Mock()
        actor.get_context.return_value = ctx
        actor._rpc_tasks = {}
        actor._ongoing_rpc_tasks = trio.Event()
        actor._ongoing_rpc_tasks.set()

        async def rpc_func():
            assert (chan, ctx.cid) in actor._rpc_tasks
            rpc_ran.set()

        async def invoke(task_status):
            await _rpc._invoke(
                actor=actor,
                cid=ctx.cid,
                chan=chan,
                func=rpc_func,
                kwargs={},
                task_status=task_status,
            )

        async with trio.open_nursery() as nursery:
            started_ctx = await nursery.start(invoke)
            assert started_ctx is ctx
            await rpc_ran.wait()

        assert rpc_ran.is_set()
        assert not actor._rpc_tasks
        assert actor._ongoing_rpc_tasks.is_set()
        chan.send.assert_awaited_once()

    trio.run(main)


def test_error_shipment_ignores_closed_response_channel(monkeypatch):
    '''
    Preserve an application error when its response channel is closed.

    A caller can disconnect after submitting an RPC but before its
    error response. Normalized `TransportClosed` from that final send
    is terminal response failure, not a new actor-wide service error.
    This test proves error shipment logs and returns without replacing
    the original application exception.

    '''
    chan = Mock()
    chan.send = AsyncMock(
        side_effect=[
            None,
            TransportClosed(
                message='caller closed before Error response',
            ),
        ],
    )
    error_msg = Mock(boxed_type_str='ValueError')
    monkeypatch.setattr(
        _rpc,
        'pack_error',
        Mock(return_value=error_msg),
    )
    ctx = Mock(
        chan=chan,
        cid='rpc-cid',
        _scope=None,
        _task='rpc-task',
    )
    actor = Mock()
    actor.get_context.return_value = ctx
    actor._rpc_tasks = {}
    actor._ongoing_rpc_tasks = trio.Event()
    actor._ongoing_rpc_tasks.set()

    async def failing_rpc():
        raise ValueError('application failure')

    async def main():
        async with trio.open_nursery() as nursery:
            started_ctx = await nursery.start(
                _rpc._invoke,
                actor,
                ctx.cid,
                chan,
                failing_rpc,
                {},
            )
            assert started_ctx is ctx

    trio.run(main)
    assert chan.send.await_count == 2
    assert not actor._rpc_tasks
    assert actor._ongoing_rpc_tasks.is_set()


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
        ) as an:

            actor = tractor.current_actor()
            assert actor.is_registrar
            await tractor.to_actor.run(
                partial(
                    sleep_back_actor,
                    actor_name=subactor_requests_to,
                    func_name=funcname,
                    func_defined=bool(func_defined),
                    exposed_mods=exposed_mods,
                    reg_addr=reg_addr,
                ),
                an=an,

                name='subactor',

                # Function from the local exposed module space the
                # subactor invokes when it RPCs back to this actor.
                enable_modules=subactor_exposed_mods,
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
