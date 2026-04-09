'''
Suites for our `.trionics.maybe_open_context()` multi-task
shared-cached `@acm` API.

'''
from contextlib import asynccontextmanager as acm
import platform
from typing import Awaitable

import pytest
import trio
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
    timeout: float = 3.6
    if platform.system() == "Windows":
        timeout: float = 10

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
    Reproduce the piker `open_cached_client('kraken')` race:

    - same `acm_func`, NO kwargs (identical `ctx_key`)
    - multiple tasks share the cached resource
    - all users exit -> teardown starts
    - a NEW task enters during `_Cache.run_ctx.__aexit__`
    - `values[ctx_key]` is gone (popped in inner finally)
      but `resources[ctx_key]` still exists (outer finally
      hasn't run yet bc the acm cleanup has checkpoints)
    - old code: `assert not resources.get(ctx_key)` FIRES

    This models the real-world scenario where `brokerd.kraken`
    tasks concurrently call `open_cached_client('kraken')`
    (same `acm_func`, empty kwargs, shared `ctx_key`) and
    the teardown/re-entry race triggers intermittently.

    '''
    async def main():
        in_aexit = trio.Event()

        @acm
        async def cached_client():
            '''
            Simulates `kraken.api.get_client()`:
            - no params (all callers share one `ctx_key`)
            - slow-ish cleanup to widen the race window
              between `values.pop()` and `resources.pop()`
              inside `_Cache.run_ctx`.

            '''
            yield 'the-client'
            # Signal that we're in __aexit__ — at this
            # point `values` has already been popped by
            # `run_ctx`'s inner finally, but `resources`
            # is still alive (outer finally hasn't run).
            in_aexit.set()
            await trio.sleep(10)

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
            `values` is popped but `resources` still exists),
            then re-enter — triggering the assert.

            '''
            await in_aexit.wait()
            async with maybe_open_context(
                cached_client,
            ) as (cache_hit, value):
                assert value == 'the-client'

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

    trio.run(main)
