'''
Deregistration-on-teardown scenarios: verify sub-actors
deregister from the registrar when cancelled or when
channels are explicitly closed.

'''
import os
import signal
import platform
from functools import partial
import itertools
import time

import psutil
import pytest
import subprocess
import tractor
from tractor.trionics import collapse_eg
import trio


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
