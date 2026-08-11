'''
Suites for our `.trionics.maybe_open_context()` multi-task
shared-cached `@acm` API.

'''
from contextlib import asynccontextmanager as acm
import platform
from typing import Awaitable

import pytest
import trio
from trio.testing import wait_all_tasks_blocked
import tractor
from tractor.trionics import (
    maybe_open_context,
    collapse_eg,
)
from tractor.log import (
    get_console_log,
    get_logger,
)

log = get_logger()

_resource: int = 0


@acm
async def maybe_increment_counter(task_name: str):
    global _resource

    _resource += 1
    await trio.lowlevel.checkpoint()
    yield _resource
    await trio.lowlevel.checkpoint()
    _resource -= 1


@pytest.mark.parametrize(
    'key_on',
    ['key_value', 'kwargs'],
    ids="key_on={}".format,
)
def test_resource_only_entered_once(key_on):
    global _resource
    _resource = 0

    key = None
    if key_on == 'key_value':
        key = 'some_common_key'

    async def main():
        cache_active: bool = False

        async def enter_cached_mngr(name: str):
            nonlocal cache_active

            if key_on == 'kwargs':
                # make a common kwargs input to key on it
                kwargs = {'task_name': 'same_task_name'}
                assert key is None
            else:
                # different task names per task will be used
                kwargs = {'task_name': name}

            async with maybe_open_context(
                maybe_increment_counter,
                kwargs=kwargs,
                key=key,

            ) as (cache_hit, resource):
                if cache_hit:
                    try:
                        cache_active = True
                        assert resource == 1
                        await trio.sleep_forever()
                    finally:
                        cache_active = False
                else:
                    assert resource == 1
                    await trio.sleep_forever()

        with trio.move_on_after(0.5):
            async with (
                tractor.open_root_actor(),
                trio.open_nursery() as tn,
            ):
                for i in range(10):
                    tn.start_soon(
                        enter_cached_mngr,
                        f'task_{i}',
                    )
                    await trio.sleep(0.001)

    trio.run(main)


def test_last_moc_user_waits_for_resource_exit():
    '''
    Verify the final user cannot return before resource teardown.

    Previously the final `maybe_open_context()` user only signalled
    `_Cache.run_ctx()` through its `no_more_users` event. The user
    then returned while the service task was still running the
    resource's `__aexit__()`, so callers could observe stale external
    state immediately after their `async with` block.

    The resource sets `exit_started` before blocking on
    `allow_exit`. The user task must remain inside MOC until the test
    releases that deterministic checkpoint and `__aexit__()` sets
    `exit_finished`.

    '''
    async def main():
        exit_started = trio.Event()
        allow_exit = trio.Event()
        exit_finished = trio.Event()
        user_returned = trio.Event()

        @acm
        async def open_resource():
            try:
                yield
            finally:
                exit_started.set()
                await allow_exit.wait()
                exit_finished.set()

        async def use_resource():
            async with maybe_open_context(open_resource):
                pass

            assert exit_finished.is_set()
            user_returned.set()

        async with (
            tractor.open_root_actor(),
            trio.open_nursery() as tn,
        ):
            tn.start_soon(use_resource)
            await exit_started.wait()
            assert not user_returned.is_set()
            allow_exit.set()
            await user_returned.wait()

    trio.run(main)


def test_moc_delivers_resource_exit_error():
    '''
    Verify a resource exit error reaches the final MOC user.

    Previously `_Cache.run_ctx()` executed the cached resource's
    `__aexit__()` after the final user had returned. An exit failure
    therefore surfaced later through the actor service nursery rather
    than at the user's `async with maybe_open_context()` boundary.

    This resource raises a unique `ResourceExitError` during exit.
    Catching that exact instance around MOC proves the service task
    delivered the failure to the final user without replacing it.

    '''
    class ResourceExitError(Exception):
        pass

    exit_error = ResourceExitError('resource exit failed')

    async def main():
        @acm
        async def open_resource():
            yield
            raise exit_error

        async with tractor.open_root_actor():
            with pytest.raises(ResourceExitError) as exc_info:
                async with maybe_open_context(open_resource):
                    pass

            assert exc_info.value is exit_error

    trio.run(main)


def test_moc_final_user_cancellation_waits_for_exit():
    '''
    Verify final-user cancellation still waits for successful exit.

    Previously cancellation escaped the final MOC user immediately
    after it signalled `_Cache.run_ctx()`, leaving resource exit to
    finish later in the actor service task. This violated the context
    manager boundary even when cleanup itself succeeded.

    The consumer cancels its own scope while holding the sole cached
    resource. The resource sets `exit_finished` from its `finally`
    block, and the consumer checks that event immediately after its
    cancel scope catches `trio.Cancelled`. This proves MOC's
    completion wait is shielded without suppressing the original
    cancellation.

    '''
    async def main():
        exit_finished = trio.Event()

        @acm
        async def open_resource():
            try:
                yield
            finally:
                exit_finished.set()

        async with tractor.open_root_actor():
            with trio.CancelScope() as cs:
                async with maybe_open_context(open_resource):
                    cs.cancel()
                    await trio.sleep_forever()

            assert cs.cancelled_caught
            assert exit_finished.is_set()

    trio.run(main)


def test_moc_exit_error_masks_final_user_cancellation():
    '''
    Verify cleanup errors survive final-user cancellation.

    A cancelled final user previously signalled `no_more_users` and
    propagated `trio.Cancelled` before `_Cache.run_ctx()` completed
    resource exit. If `__aexit__()` then failed, its error was
    detached from the API call which caused teardown.

    The consumer cancels its own scope at a deterministic checkpoint
    inside MOC. Resource exit raises `ResourceExitError`; observing
    that exact error outside the cancel scope proves MOC shields the
    completion wait and applies normal context-manager masking, where
    a cleanup failure replaces the active cancellation.

    '''
    class ResourceExitError(Exception):
        pass

    exit_error = ResourceExitError('resource exit failed')

    async def main():
        @acm
        async def open_resource():
            yield
            raise exit_error

        async with tractor.open_root_actor():
            with pytest.raises(ResourceExitError) as exc_info:
                with trio.CancelScope() as cs:
                    async with maybe_open_context(open_resource):
                        cs.cancel()
                        await trio.sleep_forever()

            assert exc_info.value is exit_error

    trio.run(main)


def test_moc_service_nursery_cancellation_completes_exit():
    '''
    Verify service-nursery cancellation cannot strand a final user.

    `_Cache.run_ctx()` and an MOC consumer may share a
    caller-provided service nursery. Cancelling that nursery
    interrupts the service task's `no_more_users` wait and the
    consumer body together. A shielded final-user wait would deadlock
    if `run_ctx()` failed to publish completion while propagating its
    own `trio.Cancelled`.

    The outer task waits for resource entry, then cancels the exact
    nursery containing both tasks. The resource shields one cleanup
    checkpoint and sets `exit_finished`; observing both it and
    `service_finished` proves cancellation propagated normally while
    MOC's completion handshake terminated deterministically.

    '''
    async def main():
        resource_entered = trio.Event()
        exit_finished = trio.Event()
        service_finished = trio.Event()
        service_tn: trio.Nursery|None = None

        @acm
        async def open_resource():
            try:
                resource_entered.set()
                yield
            finally:
                with trio.CancelScope(shield=True):
                    await trio.lowlevel.checkpoint()
                    exit_finished.set()

        async def use_resource(tn: trio.Nursery):
            async with maybe_open_context(
                open_resource,
                tn=tn,
            ):
                await trio.sleep_forever()

        async def run_service():
            nonlocal service_tn

            async with trio.open_nursery() as tn:
                service_tn = tn
                tn.start_soon(use_resource, tn)

            service_finished.set()

        async with trio.open_nursery() as outer_tn:
            outer_tn.start_soon(run_service)
            await resource_entered.wait()
            assert service_tn is not None
            service_tn.cancel_scope.cancel()
            await service_finished.wait()

        assert exit_finished.is_set()

    trio.run(main)


@tractor.context
async def streamer(
    ctx: tractor.Context,
    seq: list[int] = list(range(1000)),
) -> None:

    await ctx.started()
    async with ctx.open_stream() as stream:
        for val in seq:
            await stream.send(val)
            await trio.sleep(0.001)

    print('producer finished')


@acm
async def open_stream() -> Awaitable[
    tuple[
        tractor.ActorNursery,
        tractor.MsgStream,
    ]
]:
    try:
        async with tractor.open_nursery() as an:
            portal = await an.start_actor(
                'streamer',
                enable_modules=[__name__],
            )
            try:
                async with (
                    portal.open_context(streamer) as (ctx, first),
                    ctx.open_stream() as stream,
                ):
                    print('Entered open_stream() caller')
                    yield an, stream
                    print('Exited open_stream() caller')

            finally:
                print(
                    'Cancelling streamer with,\n'
                    '=> `Portal.cancel_actor()`'
                )
                await portal.cancel_actor()
                print('Cancelled streamer')

    except Exception as err:
        print(
            f'`open_stream()` errored?\n'
            f'{err!r}\n'
        )
        await tractor.pause(shield=True)
        raise err


@acm
async def maybe_open_stream(taskname: str):
    async with maybe_open_context(
        # NOTE: all secondary tasks should cache hit on the same key
        acm_func=open_stream,
    ) as (
        cache_hit,
        (an, stream)
    ):
        # when the actor + portal + ctx + stream has already been
        # allocated we want to just bcast to this task.
        if cache_hit:
            print(f'{taskname} loaded from cache')

            # add a new broadcast subscription for the quote stream
            # if this feed is already allocated by the first
            # task that entereed
            async with stream.subscribe() as bstream:
                yield an, bstream
                print(
                    f'cached task exited\n'
                    f')>\n'
                    f' |_{taskname}\n'
                )

            # we should always unreg the "cloned" bcrc for this
            # consumer-task
            assert id(bstream) not in bstream._state.subs

        else:
            # yield the actual stream
            try:
                yield an, stream
            finally:
                print(
                    f'NON-cached task exited\n'
                    f')>\n'
                    f' |_{taskname}\n'
                )

        first_bstream = stream._broadcaster
        bcrx_state = first_bstream._state
        subs: dict[int, int] = bcrx_state.subs
        if len(subs) == 1:
            assert id(first_bstream) in subs
            # ^^TODO! the bcrx should always de-allocate all subs,
            # including the implicit first one allocated on entry
            # by the first subscribing peer task, no?
            #
            # -[ ] adjust `MsgStream.subscribe()` to do this mgmt!
            #  |_ allows reverting `MsgStream.receive()` to the
            #    non-bcaster method.
            #  |_ we can decide whether to reset `._broadcaster`?
            #
            # await tractor.pause(shield=True)


def test_open_local_sub_to_stream(
    debug_mode: bool,
):
    '''
    Verify a single inter-actor stream can can be fanned-out shared to
    N local tasks using `trionics.maybe_open_context()`.

    '''
    from .conftest import cpu_perf_headroom
    timeout: float = (
        4
        if not platform.system() == "Windows"
        else 10
    ) * cpu_perf_headroom()

    if debug_mode:
        timeout = 999
        print(f'IN debug_mode, setting large timeout={timeout!r}..')

    async def main():

        full = list(range(1000))
        an: tractor.ActorNursery|None = None
        num_tasks: int = 10

        async def get_sub_and_pull(taskname: str):

            nonlocal an

            stream: tractor.MsgStream
            async with (
                maybe_open_stream(taskname) as (
                    an,
                    stream,
                ),
            ):
                if '0' in taskname:
                    assert isinstance(stream, tractor.MsgStream)
                else:
                    assert isinstance(
                        stream,
                        tractor.trionics.BroadcastReceiver
                    )

                first = await stream.receive()
                print(f'{taskname} started with value {first}')
                seq: list[int] = []
                async for msg in stream:
                    seq.append(msg)

                assert set(seq).issubset(set(full))

            # end of @acm block
            print(f'{taskname} finished')

        root: tractor.Actor
        with trio.fail_after(timeout) as cs:
            # TODO: turns out this isn't multi-task entrant XD
            # We probably need an indepotent entry semantic?
            async with tractor.open_root_actor(
                debug_mode=debug_mode,
                # maybe_enable_greenback=True,
                #
                # ^TODO? doesn't seem to mk breakpoint() usage work
                # bc each bg task needs to open a portal??
                # - [ ] we should consider making this part of
                #      our taskman defaults?
                #   |_see https://github.com/goodboy/tractor/pull/363
                #
            ) as root:
                assert root.is_registrar

                async with (
                    trio.open_nursery() as tn,
                ):
                    for i in range(num_tasks):
                        tn.start_soon(
                            get_sub_and_pull,
                            f'task_{i}',
                        )
                        await trio.sleep(0.001)

                print('all consumer tasks finished!')

                # ?XXX, ensure actor-nursery is shutdown or we might
                # hang here due to a minor task deadlock/race-condition?
                #
                # - seems that all we need is a checkpoint to ensure
                #   the last suspended task, which is inside
                #   `.maybe_open_context()`, can do the
                #   `Portal.cancel_actor()` call?
                #
                # - if that bg task isn't resumed, then this blocks
                #   timeout might hit before that?
                #
                if root.ipc_server.has_peers():
                    await trio.lowlevel.checkpoint()

                    # alt approach, cancel the entire `an`
                    # await tractor.pause()
                    # await an.cancel()

            # end of runtime scope
            print('root actor terminated.')

        if cs.cancelled_caught:
            pytest.fail(
                'Should NOT time out in `open_root_actor()` ?'
            )

        print('exiting main.')

    trio.run(main)



@acm
async def maybe_cancel_outer_cs(
    cs: trio.CancelScope|None = None,
    delay: float = 0,
):
    # on first task delay this enough to block
    # the 2nd task but then cancel it mid sleep
    # so that the tn.start() inside the key-err handler block
    # is cancelled and would previously corrupt the
    # mutext state.
    log.info(f'task entering sleep({delay})')
    await trio.sleep(delay)
    if cs:
        log.info('task calling cs.cancel()')
        cs.cancel()

    yield

    if cs:
        await trio.sleep_forever()

    # XXX, if not cancelled we'll leak this inf-blocking
    # subtask to the actor's service tn..
    else:
        await trio.lowlevel.checkpoint()


@pytest.mark.parametrize(
    'delay',
    [0.05, 0.5, 1],
    ids="pre_sleep_delay={}".format,
)
@pytest.mark.parametrize(
    'cancel_by_cs',
    [True, False],
    ids="cancel_by_cs={}".format,
)
def test_lock_not_corrupted_on_fast_cancel(
    delay: float,
    cancel_by_cs: bool,
    debug_mode: bool,
    loglevel: str,
):
    '''
    Verify that if the caching-task (the first to enter
    `maybe_open_context()`) is cancelled mid-cache-miss, the embedded
    mutex can never be left in a corrupted state.

    That is, the lock is always eventually released ensuring a peer
    (cache-hitting) task will never,

    - be left to inf-block/hang on the `lock.acquire()`.
    - try to release the lock when still owned by the caching-task
      due to it having erronously exited without calling
      `lock.release()`.

    '''
    async def use_moc(
        delay: float,
        cs: trio.CancelScope|None = None,
    ):
        log.info('task entering moc')
        async with maybe_open_context(
            maybe_cancel_outer_cs,
            kwargs={
                'cs': cs,
                'delay': delay,
            },
        ) as (cache_hit, _null):
            if cache_hit:
                log.info('2nd task entered')
            else:
                log.info('1st task entered')

            if cs:
                await trio.sleep_forever()

            else:
                await trio.sleep(delay)

        # ^END, exit shared ctx.

    async def main():
        with trio.fail_after(delay + 2):
            async with (
                tractor.open_root_actor(
                    debug_mode=debug_mode,
                    loglevel=loglevel,
                ),
                # ?TODO, pass this as the parent tn?
                trio.open_nursery() as tn,
            ):
                get_console_log('info')
                log.info('yo starting')
                cs = tn.cancel_scope
                tn.start_soon(
                    use_moc,
                    delay,
                    cs if cancel_by_cs else None,
                    name='child',
                )
                with trio.CancelScope() as rent_cs:
                    await use_moc(
                        delay=delay,
                        cs=rent_cs if cancel_by_cs else None,
                    )

    trio.run(main)


@acm
async def acm_with_resource(resource_id: str):
    '''
    Yield `resource_id` as the cached value.

    Used to verify per-`ctx_key` isolation when the same
    `acm_func` is called with different kwargs.

    '''
    yield resource_id


def test_per_ctx_key_resource_lifecycle(
    debug_mode: bool,
    loglevel: str,
):
    '''
    Verify that `maybe_open_context()` correctly isolates resource
    lifecycle **per `ctx_key`** when the same `acm_func` is called
    with different kwargs.

    Previously `_Cache.users` was a single global `int` and
    `_Cache.locks` was keyed on `fid` (function ID), so calling
    the same `acm_func` with different kwargs (producing different
    `ctx_key`s) meant:

    - teardown for one key was skipped bc the *other* key's users
      kept the global count > 0,
    - and re-entry could hit the old
      `assert not resources.get(ctx_key)` crash during the
      teardown window.

    This was the root cause of a long-standing bug in piker's
    `brokerd.kraken` backend.

    '''
    timeout: float = 6
    if debug_mode:
        timeout = 999

    async def main():
        a_ready = trio.Event()
        a_exit = trio.Event()

        async def hold_resource_a():
            '''
            Open resource 'a' and keep it alive until signalled.

            '''
            async with maybe_open_context(
                acm_with_resource,
                kwargs={'resource_id': 'a'},
            ) as (cache_hit, value):
                assert not cache_hit
                assert value == 'a'
                log.info("resource 'a' entered (holding)")
                a_ready.set()
                await a_exit.wait()
                log.info("resource 'a' exiting")

        with trio.fail_after(timeout):
            async with (
                tractor.open_root_actor(
                    debug_mode=debug_mode,
                    loglevel=loglevel,
                ),
                trio.open_nursery() as tn,
            ):
                # Phase 1: bg task holds resource 'a' open.
                tn.start_soon(hold_resource_a)
                await a_ready.wait()

                # Phase 2: open resource 'b' (different kwargs,
                # same acm_func) then exit it while 'a' is still
                # alive.
                async with maybe_open_context(
                    acm_with_resource,
                    kwargs={'resource_id': 'b'},
                ) as (cache_hit, value):
                    assert not cache_hit
                    assert value == 'b'
                    log.info("resource 'b' entered")

                log.info("resource 'b' exited, waiting for teardown")
                await trio.lowlevel.checkpoint()

                # Phase 3: re-open 'b'; must be a fresh cache MISS
                # proving 'b' was torn down independently of 'a'.
                #
                # With the old global `_Cache.users` counter this
                # would be a stale cache HIT (leaked resource) or
                # trigger `assert not resources.get(ctx_key)`.
                async with maybe_open_context(
                    acm_with_resource,
                    kwargs={'resource_id': 'b'},
                ) as (cache_hit, value):
                    assert not cache_hit, (
                        "resource 'b' was NOT torn down despite "
                        "having zero users! (global user count bug)"
                    )
                    assert value == 'b'
                    log.info(
                        "resource 'b' re-entered "
                        "(cache miss, correct)"
                    )

                # Phase 4: let 'a' exit, clean shutdown.
                a_exit.set()

    trio.run(main)


def test_moc_reentry_during_teardown(
    debug_mode: bool,
    loglevel: str,
):
    '''
    Reproduce re-entry while an identical cached context exits.

    - multiple tasks use the same `acm_func` with no kwargs,
      producing an identical `ctx_key`;
    - all users leave and the final user starts resource teardown;
    - `_Cache.run_ctx()` removes the cached value and resource entry
      before entering the resource's blocking `__aexit__()` body;
    - a new task attempts to enter that same `ctx_key` during exit;
    - the per-key lock keeps that entrant queued until exit
      completes;
    - the entrant then receives a fresh cache miss and resource.

    Without teardown sharing the registration lock, re-entry could
    race resource replacement while the prior generation was still
    exiting. The final user could also return before that exit
    completed.

    The first resource generation signals `in_aexit` and waits on
    `allow_aexit`. The re-entry task signals `reentry_started` and
    blocks inside MOC; only after `wait_all_tasks_blocked()` confirms
    that ordering does the coordinator release cleanup. The entrant
    must then receive a fresh cache miss. `first_done` additionally
    proves the first MOC user observed completed teardown before
    returning.

    '''
    async def main():
        in_aexit = trio.Event()
        allow_aexit = trio.Event()
        reentry_started = trio.Event()
        generation: int = 0

        @acm
        async def cached_client():
            '''
            Simulate a no-argument `kraken.api.get_client()`.

            '''
            nonlocal generation

            generation += 1
            resource_generation: int = generation
            yield 'the-client'
            if resource_generation == 1:
                in_aexit.set()
                await allow_aexit.wait()

        first_done = trio.Event()

        async def use_and_exit():
            async with maybe_open_context(
                cached_client,
            ) as (cache_hit, value):
                assert value == 'the-client'
            first_done.set()

        async def reenter_during_teardown():
            '''
            Wait for the acm's `__aexit__` to start (meaning
            the cached value is no longer available), then re-enter.

            '''
            await in_aexit.wait()

            # Tell the coordinator this task is about to enter MOC.
            # `Event.set()` is not a checkpoint. Though `async with`
            # awaits MOC's `__aenter__()`, its async generator runs
            # synchronously until the held per-key `lock.acquire()`
            # actually suspends this task.
            reentry_started.set()
            async with maybe_open_context(
                cached_client,
            ) as (cache_hit, value):
                assert not cache_hit
                assert value == 'the-client'

            await first_done.wait()

        with trio.fail_after(5):
            async with (
                tractor.open_root_actor(
                    debug_mode=debug_mode,
                    loglevel=loglevel,
                ),
                collapse_eg(),
                trio.open_nursery() as tn,
            ):
                tn.start_soon(use_and_exit)
                tn.start_soon(reenter_during_teardown)
                await reentry_started.wait()

                # Wait until the re-entry task is queued on MOC's
                # per-key lock while `_Cache.run_ctx()` remains
                # blocked in the first generation's `__aexit__()`.
                # Only then release cleanup, making the intended
                # enter-during-sibling-exit ordering deterministic.
                await wait_all_tasks_blocked()
                assert not first_done.is_set()
                allow_aexit.set()

    trio.run(main)
