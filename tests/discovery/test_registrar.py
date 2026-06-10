'''
Discovery subsystem via a "registrar" actor scenarios.

'''
import os
import signal
import platform
from functools import partial
import itertools
import time
from typing import Callable

import psutil
import pytest
import subprocess
import tractor
from tractor.devx import dump_on_hang
from tractor.trionics import collapse_eg
from tractor._testing import tractor_test
from tractor.discovery._addr import wrap_address
from tractor.discovery._multiaddr import mk_maddr
import trio


@tractor_test
async def test_reg_then_unreg(
    reg_addr: tuple,
):
    actor = tractor.current_actor()
    assert actor.is_registrar
    assert len(actor._registry) == 1  # only self is registered

    async with tractor.open_nursery(
        registry_addrs=[reg_addr],
    ) as n:

        portal = await n.start_actor('actor', enable_modules=[__name__])
        uid = portal.channel.aid.uid

        async with tractor.get_registry(reg_addr) as aportal:
            # this local actor should be the registrar
            assert actor is aportal.actor

            async with tractor.wait_for_actor('actor'):
                # sub-actor uid should be in the registry
                assert uid in aportal.actor._registry
                sockaddrs = actor._registry[uid]
                # XXX: can we figure out what the listen addr will be?
                assert sockaddrs

        await n.cancel()  # tear down nursery

        await trio.sleep(0.1)
        assert uid not in aportal.actor._registry
        sockaddrs = actor._registry.get(uid)
        assert not sockaddrs


@tractor_test
async def test_reg_then_unreg_maddr(
    reg_addr: tuple,
):
    '''
    Same as `test_reg_then_unreg` but pass the registry
    address as a multiaddr string to verify `wrap_address()`
    multiaddr parsing end-to-end through the runtime.

    '''
    # tuple -> Address -> multiaddr string
    addr_obj = wrap_address(reg_addr)
    maddr_str: str = str(mk_maddr(addr_obj))

    actor = tractor.current_actor()
    assert actor.is_registrar

    async with tractor.open_nursery(
        registry_addrs=[maddr_str],
    ) as n:

        portal = await n.start_actor(
            'actor_maddr',
            enable_modules=[__name__],
        )
        uid = portal.channel.aid.uid

        async with tractor.get_registry(maddr_str) as aportal:
            assert actor is aportal.actor

            async with tractor.wait_for_actor('actor_maddr'):
                assert uid in aportal.actor._registry
                sockaddrs = actor._registry[uid]
                assert sockaddrs

        await n.cancel()

        await trio.sleep(0.1)
        assert uid not in aportal.actor._registry
        sockaddrs = actor._registry.get(uid)
        assert not sockaddrs


the_line = 'Hi my name is {}'


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(
    other_actor: str,
    reg_addr: tuple[str, int],
):
    await trio.sleep(1)  # wait for other actor to spawn
    async with tractor.find_actor(
        other_actor,
        registry_addrs=[reg_addr],
    ) as portal:
        assert portal is not None
        return await portal.run(__name__, 'hi')


async def say_hello_use_wait(
    other_actor: str,
    reg_addr: tuple[str, int],
):
    async with tractor.wait_for_actor(
        other_actor,
        registry_addr=reg_addr,
    ) as portal:
        assert portal is not None
        result = await portal.run(__name__, 'hi')
        return result


@tractor_test
@pytest.mark.parametrize(
    'func',
    [say_hello,
     say_hello_use_wait]
)
async def test_trynamic_trio(
    func: Callable,
    start_method: str,
    reg_addr: tuple,
):
    '''
    Root actor acting as the "director" and running one-shot-task-actors
    for the directed subs.

    '''
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny = await n.run_in_actor(
            func,
            other_actor='gretchen',
            reg_addr=reg_addr,
            name='donny',
        )
        gretchen = await n.run_in_actor(
            func,
            other_actor='donny',
            reg_addr=reg_addr,
            name='gretchen',
        )
        print(await gretchen.result())
        print(await donny.result())
        print("CUTTTT CUUTT CUT!!?! Donny!! You're supposed to say...")


async def stream_forever():
    for i in itertools.count():
        yield i
        await trio.sleep(0.01)


async def cancel(
    use_signal: bool,
    delay: float = 0,
):
    # hold on there sally
    await trio.sleep(delay)

    # trigger cancel
    if use_signal:
        if platform.system() == 'Windows':
            pytest.skip("SIGINT not supported on windows")
        os.kill(os.getpid(), signal.SIGINT)
    else:
        raise KeyboardInterrupt


async def stream_from(portal: tractor.Portal):
    async with portal.open_stream_from(stream_forever) as stream:
        async for value in stream:
            print(value)


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


async def spawn_and_check_registry(
    reg_addr: tuple,
    use_signal: bool,
    debug_mode: bool = False,
    remote_arbiter: bool = False,
    with_streaming: bool = False,
    maybe_daemon: tuple[
        subprocess.Popen,
        psutil.Process,
    ]|None = None,

) -> None:

    if maybe_daemon:
        popen, proc = maybe_daemon
        # breakpoint()

    async with tractor.open_root_actor(
        registry_addrs=[reg_addr],
        debug_mode=debug_mode,
    ):
        async with tractor.get_registry(
            addr=reg_addr,
        ) as portal:
            # runtime needs to be up to call this
            actor = tractor.current_actor()

            if remote_arbiter:
                assert not actor.is_registrar

            if actor.is_registrar:
                extra = 1  # registrar is local root actor
                get_reg = partial(unpack_reg, actor)

            else:
                get_reg = partial(unpack_reg, portal)
                extra = 2  # local root actor + remote registrar

            # ensure current actor is registered
            registry: dict = await get_reg()
            assert actor.aid.uid in registry

            try:
                async with tractor.open_nursery() as an:
                    async with (
                        collapse_eg(),
                        trio.open_nursery() as trion,
                    ):
                        portals = {}
                        for i in range(3):
                            name = f'a{i}'
                            if with_streaming:
                                portals[name] = await an.start_actor(
                                    name=name, enable_modules=[__name__])

                            else:  # no streaming
                                portals[name] = await an.run_in_actor(
                                    trio.sleep_forever, name=name)

                        # wait on last actor to come up
                        async with tractor.wait_for_actor(name):
                            registry = await get_reg()
                            for uid in an._children:
                                assert uid in registry

                        assert len(portals) + extra == len(registry)

                        if with_streaming:
                            await trio.sleep(0.1)

                            pts = list(portals.values())
                            for p in pts[:-1]:
                                trion.start_soon(stream_from, p)

                            # stream for 1 sec
                            trion.start_soon(cancel, use_signal, 1)

                            last_p = pts[-1]
                            await stream_from(last_p)

                        else:
                            await cancel(use_signal)

            finally:
                await trio.sleep(0.5)

                # all subactors should have de-registered
                registry = await get_reg()
                start: float = time.time()
                while (
                    not (len(registry) == extra)
                    and
                    (time.time() - start) < 5
                ):
                    print(
                        f'Waiting for remaining subs to dereg..\n'
                        f'{registry!r}\n'
                    )
                    await trio.sleep(0.3)
                else:
                    assert len(registry) == extra

                assert actor.aid.uid in registry


@pytest.mark.parametrize('use_signal', [False, True])
@pytest.mark.parametrize('with_streaming', [False, True])
def test_subactors_unregister_on_cancel(
    debug_mode: bool,
    start_method: str,
    use_signal: bool,
    reg_addr: tuple,
    with_streaming: bool,
):
    '''
    Verify that cancelling a nursery results in all subactors
    deregistering themselves with the registrar.

    '''
    with pytest.raises(KeyboardInterrupt):
        trio.run(
            partial(
                spawn_and_check_registry,
                reg_addr,
                use_signal,
                debug_mode=debug_mode,
                remote_arbiter=False,
                with_streaming=with_streaming,
            ),
        )


@pytest.mark.parametrize('use_signal', [False, True])
@pytest.mark.parametrize('with_streaming', [False, True])
def test_subactors_unregister_on_cancel_remote_daemon(
    daemon: subprocess.Popen,
    debug_mode: bool,
    start_method: str,
    use_signal: bool,
    reg_addr: tuple,
    with_streaming: bool,
):
    '''
    Verify that cancelling a nursery results in all subactors
    deregistering themselves with a **remote** (not in the local
    process tree) registrar.

    '''
    with pytest.raises(KeyboardInterrupt):
        trio.run(
            partial(
                spawn_and_check_registry,
                reg_addr,
                use_signal,
                debug_mode=debug_mode,
                remote_arbiter=True,
                with_streaming=with_streaming,
                maybe_daemon=(
                    daemon,
                    psutil.Process(daemon.pid)
                ),
            ),
        )


async def streamer(agen):
    async for item in agen:
        print(item)


async def close_chans_before_nursery(
    reg_addr: tuple,
    use_signal: bool,
    remote_arbiter: bool = False,
) -> None:

    # logic for how many actors should still be
    # in the registry at teardown.
    if remote_arbiter:
        entries_at_end = 2
    else:
        entries_at_end = 1

    async with tractor.open_root_actor(
        registry_addrs=[reg_addr],
    ):
        async with tractor.get_registry(reg_addr) as aportal:
            try:
                get_reg = partial(unpack_reg, aportal)

                async with tractor.open_nursery() as an:
                    portal1 = await an.start_actor(
                        name='consumer1',
                        enable_modules=[__name__],
                    )
                    portal2 = await an.start_actor(
                        'consumer2',
                        enable_modules=[__name__],
                    )

                    async with (
                        portal1.open_stream_from(
                            stream_forever
                        ) as agen1,
                        portal2.open_stream_from(
                            stream_forever
                        ) as agen2,
                    ):
                            async with (
                                collapse_eg(),
                                trio.open_nursery() as tn,
                            ):
                                tn.start_soon(streamer, agen1)
                                tn.start_soon(cancel, use_signal, .5)
                                try:
                                    await streamer(agen2)
                                finally:
                                    # Kill the root nursery thus resulting in
                                    # normal registrar channel ops to fail during
                                    # teardown. It doesn't seem like this is
                                    # reliably triggered by an external SIGINT.
                                    # tractor.current_actor()._root_nursery.cancel_scope.cancel()

                                    # XXX: THIS IS THE KEY THING that
                                    # happens **before** exiting the
                                    # actor nursery block

                                    # also kill off channels cuz why not
                                    await agen1.aclose()
                                    await agen2.aclose()

            finally:
                with trio.CancelScope(shield=True):
                    await trio.sleep(1)

                    # all subactors should have de-registered
                    registry = await get_reg()
                    assert portal1.channel.aid.uid not in registry
                    assert portal2.channel.aid.uid not in registry
                    assert len(registry) == entries_at_end


@pytest.mark.parametrize('use_signal', [False, True])
def test_close_channel_explicit(
    start_method: str,
    use_signal: bool,
    reg_addr: tuple,
):
    '''
    Verify that closing a stream explicitly and killing the actor's
    "root nursery" **before** the containing nursery tears down also
    results in subactor(s) deregistering from the registrar.

    '''
    with pytest.raises(KeyboardInterrupt):
        trio.run(
            partial(
                close_chans_before_nursery,
                reg_addr,
                use_signal,
                remote_arbiter=False,
            ),
        )


@pytest.mark.parametrize('use_signal', [False, True])
def test_close_channel_explicit_remote_registrar(
    daemon: subprocess.Popen,
    start_method: str,
    use_signal: bool,
    reg_addr: tuple,
):
    '''
    Verify that closing a stream explicitly and killing the actor's
    "root nursery" **before** the containing nursery tears down also
    results in subactor(s) deregistering from the registrar.

    '''
    with pytest.raises(KeyboardInterrupt):
        trio.run(
            partial(
                close_chans_before_nursery,
                reg_addr,
                use_signal,
                remote_arbiter=True,
            ),
        )


@tractor.context
async def kill_transport(
    ctx: tractor.Context,
) -> None:

    await ctx.started()
    actor: tractor.Actor = tractor.current_actor()
    actor.ipc_server.cancel()
    await trio.sleep_forever()



# @pytest.mark.parametrize('use_signal', [False, True])
#
# Wall-clock bound via `pytest-timeout` (`method='thread'`).
# Under `--spawn-backend=subint` this test can wedge in an
# un-Ctrl-C-able state (abandoned-subint + shared-GIL
# starvation → signal-wakeup-fd pipe fills → SIGINT silently
# dropped; see `ai/conc-anal/subint_sigint_starvation_issue.md`).
# `method='thread'` is specifically required because `signal`-
# method SIGALRM suffers the same GIL-starvation path and
# wouldn't fire the Python-level handler.
# At timeout the plugin hard-kills the pytest process — that's
# the intended behavior here; the alternative is an unattended
# suite run that never returns.
@pytest.mark.timeout(
    3,  # NOTE should be a 2.1s happy path.
    method='thread',
)
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
                    # `._api.maybe_open_portal()` call should
                    # fail and implicitly call `.delete_addr()`
                    assert maybe_portal is None
                    registry: dict = await unpack_reg(_reg_ptl)
                    assert ptl.chan.aid.uid not in registry

                # should fail since we knocked out the IPC tpt XD
                await ptl.cancel_actor()
                await an.cancel()

    # TODO, remove once the `[subint]` variant no longer hangs.
    #
    # Status (as of Phase B hard-kill landing):
    #
    # - `[trio]`/`[mp_*]` variants: completes normally; `dump_on_hang`
    #   is a no-op safety net here.
    #
    # - `[subint]` variant: hangs indefinitely AND is un-Ctrl-C-able.
    #   `strace -p <pytest_pid>` while in the hang reveals a silently-
    #   dropped SIGINT — the C signal handler tries to write the
    #   signum byte to Python's signal-wakeup fd and gets `EAGAIN`,
    #   meaning the pipe is full (nobody's draining it).
    #
    #   Root-cause chain: our hard-kill in `spawn._subint` abandoned
    #   the driver OS-thread (which is `daemon=True`) after the soft-
    #   kill timeout, but the *sub-interpreter* inside that thread is
    #   still running `trio.run()` — `_interpreters.destroy()` can't
    #   force-stop a running subint (raises `InterpreterError`), and
    #   legacy-config subints share the main GIL. The abandoned subint
    #   starves the parent's trio event loop from iterating often
    #   enough to drain its wakeup pipe → SIGINT silently drops.
    #
    #   This is structurally a CPython-level limitation: there's no
    #   public force-destroy primitive for a running subint. We
    #   escape on the harness side via a SIGINT-loop in the `daemon`
    #   fixture teardown (killing the bg registrar subproc closes its
    #   end of the IPC, which eventually unblocks a recv in main trio,
    #   which lets the loop drain the wakeup pipe). Long-term fix path:
    #   msgspec PEP 684 support (jcrist/msgspec#563) → isolated-mode
    #   subints with per-interp GIL.
    #
    #   Full analysis:
    #   `ai/conc-anal/subint_sigint_starvation_issue.md`
    #
    #   See also the *sibling* hang class documented in
    #   `ai/conc-anal/subint_cancel_delivery_hang_issue.md` — same
    #   subint backend, different root cause (Ctrl-C-able hang, main
    #   trio loop iterating fine; ours to fix, not CPython's).
    #   Reproduced by `tests/test_subint_cancellation.py
    #   ::test_subint_non_checkpointing_child`.
    #
    # Kept here (and not behind a `pytestmark.skip`) so we can still
    # inspect the dump file if the hang ever returns after a refactor.
    # `pytest`'s stderr capture eats `faulthandler` output otherwise,
    # so we route `dump_on_hang` to a file.
    with dump_on_hang(
        seconds=20,
        path=f'/tmp/test_stale_entry_is_deleted_{start_method}.dump',
    ):
        trio.run(main)
