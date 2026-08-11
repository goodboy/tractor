# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Async context manager primitives with hard ``trio``-aware semantics

'''
from __future__ import annotations
from collections import defaultdict
from contextlib import (
    asynccontextmanager as acm,
)
import inspect
from types import ModuleType
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Hashable,
    Sequence,
    TypeVar,
    TYPE_CHECKING,
)

import trio
from tractor.runtime._state import current_actor
from tractor.log import get_logger
# from ._beg import collapse_eg
# from ._taskc import (
#     maybe_raise_from_masking_exc,
# )


if TYPE_CHECKING:
    from tractor import ActorNursery


log = get_logger()

# A regular invariant generic type
T = TypeVar("T")


@acm
async def maybe_open_nursery(
    nursery: trio.Nursery|ActorNursery|None = None,
    shield: bool = False,
    lib: ModuleType = trio,

    **kwargs,  # proxy thru

) -> AsyncGenerator[trio.Nursery, Any]:
    '''
    Create a new nursery if None provided.

    Blocks on exit as expected if no input nursery is provided.

    '''
    if nursery is not None:
        yield nursery
    else:
        async with lib.open_nursery(**kwargs) as nursery:
            if lib == trio:
                nursery.cancel_scope.shield = shield
            yield nursery


async def _enter_and_wait(
    mngr: AsyncContextManager[T],
    unwrapped: dict[int, T],
    all_entered: trio.Event,
    parent_exit: trio.Event,
    seed: int,

) -> None:
    '''
    Open the async context manager deliver it's value
    to this task's spawner and sleep until cancelled.

    '''
    async with mngr as value:
        unwrapped[id(mngr)] = value

        if all(
            val != seed
            for val in unwrapped.values()
        ):
            all_entered.set()

        await parent_exit.wait()


@acm
async def gather_contexts(
    mngrs: Sequence[AsyncContextManager[T]],

    # caller can provide their own scope
    tn: trio.Nursery|None = None,

) -> AsyncGenerator[
    tuple[
        T | None,
        ...
    ],
    None,
]:
    '''
    Concurrently enter a sequence of async context managers (`acm`\\ s),
    each scheduled in a separate `trio.Task` and deliver their
    unwrapped `yield`-ed values in the same order once all `@acm`\\ s
    in every task have entered.

    On exit, all `acm`\\ s are subsequently and concurrently exited with
    **no order guarantees**.

    This function is somewhat similar to a batch of non-blocking
    calls to `contextlib.AsyncExitStack.enter_async_context()`
    (inside a loop) *in combo with* a `asyncio.gather()` to get the
    `.__aenter__()`-ed values, except the managers are both
    concurrently entered and exited and *cancellation-just-works™*.

    '''
    seed: int = id(mngrs)
    unwrapped: dict[int, T|None] = {}.fromkeys(
        (id(mngr) for mngr in mngrs),
        seed,
    )

    all_entered = trio.Event()
    parent_exit = trio.Event()

    # XXX: ensure greedy sequence of manager instances
    # since a lazy inline generator doesn't seem to work
    # with `async with` syntax.
    mngrs = list(mngrs)

    if not mngrs:
        raise ValueError(
            '`.trionics.gather_contexts()` input mngrs is empty?\n'
            '\n'
            'Did try to use inline generator syntax?\n'
            'Check that list({mngrs}) works!\n'
            # 'or sequence-type intead!\n'
            # 'Use a non-lazy iterator or sequence-type intead!\n'
        )

    try:
        async with (
            #
            # ?TODO, does including these (eg-collapsing,
            # taskc-unmasking) improve tb noise-reduction/legibility?
            #
            # collapse_eg(),
            maybe_open_nursery(
                nursery=tn,
            ) as tn,
            # maybe_raise_from_masking_exc(),
        ):
            for mngr in mngrs:
                tn.start_soon(
                    _enter_and_wait,
                    mngr,
                    unwrapped,
                    all_entered,
                    parent_exit,
                    seed,
                )

            # deliver control to caller once all ctx-managers have
            # started (yielded back to us).
            await all_entered.wait()
            yield tuple(unwrapped.values())
            parent_exit.set()

    finally:
        # XXX NOTE: this is ABSOLUTELY REQUIRED to avoid
        # the following wacky bug:
        # <tractorbugurlhere>
        parent_exit.set()


# Per actor task caching helpers.
# Further potential examples of interest:
# https://gist.github.com/njsmith/cf6fc0a97f53865f2c671659c88c1798#file-cache-py-L8

class _CtxExit:
    '''
    Completion state for a cached context's shared exit.

    '''
    def __init__(self) -> None:
        self.done = trio.Event()
        self.error: Exception|None = None


class _Cache:
    '''
    Globally (actor-processs scoped) cached, task access to
    a kept-alive-while-in-use async resource.

    '''
    service_tn: trio.Nursery|None = None
    locks: dict[Hashable, trio.StrictFIFOLock] = {}
    users: defaultdict[
        tuple|Hashable,
        int,
    ] = defaultdict(int)
    values: dict[Any,  Any] = {}
    resources: dict[
        Hashable,
        tuple[
            trio.Nursery,
            trio.Event,
            _CtxExit,
        ],
    ] = {}
    # nurseries: dict[int, trio.Nursery] = {}
    no_more_users: trio.Event|None = None

    @classmethod
    async def run_ctx(
        cls,
        mng,
        ctx_key: tuple,
        ctx_exit: _CtxExit,
        task_status: trio.TaskStatus[T] = trio.TASK_STATUS_IGNORED,

    ) -> None:
        entered: bool = False
        try:
            async with mng as value:
                entered = True
                (
                    _,
                    no_more_users,
                    _,
                ) = cls.resources[ctx_key]
                cls.values[ctx_key] = value
                task_status.started(value)
                try:
                    await no_more_users.wait()
                finally:
                    cls.values.pop(ctx_key)
                    cls.resources.pop(ctx_key)

        except Exception as exc:
            if not entered:
                raise

            # Deliver regular `__aexit__()` failures to the final
            # consumer instead of raising into the service nursery.
            ctx_exit.error = exc

        finally:
            if entered:
                ctx_exit.done.set()


class _UnresolvedCtx:
    '''
    Placeholder for the maybe-value delivered from some `acm_func`,
    once (first) entered by a `maybe_open_context()` task.

    Enables internal teardown logic conditioned on whether the
    context was actually entered successfully vs. cancelled prior.

    '''


@acm
async def maybe_open_context(
    acm_func: Callable[..., AsyncContextManager[T]],

    # XXX: used as cache key after conversion to tuple
    # and all embedded values must also be hashable
    kwargs: dict = {},
    key: Hashable|Callable[..., Hashable] = None,

    # caller can provide their own scope
    tn: trio.Nursery|None = None,

) -> AsyncIterator[tuple[bool, T]]:
    '''
    Maybe open an async-context-manager (acm) if there is not already
    a `_Cached` version for the provided (input) `key` for *this* actor.

    Return the `_Cached` instance on a _Cache hit.

    '''
    fid: int = id(acm_func)
    if inspect.isfunction(key):
        ctx_key = (
            fid,
            key(**kwargs)
        )
    else:
        ctx_key = (
            fid,
            key or tuple(kwargs.items())
        )

    # yielded output
    yielded: Any = _UnresolvedCtx
    user_registered: bool = False
    ctx_exit: _CtxExit|None = None
    exit_error: Exception|None = None

    # Lock resource acquisition around task racing  / ``trio``'s
    # scheduler protocol.
    # NOTE: the lock is target context manager func specific in order
    # to allow re-entrant use cases where one `maybe_open_context()`
    # wrapped factory may want to call into another.
    task: trio.Task = trio.lowlevel.current_task()
    lock: trio.StrictFIFOLock|None = _Cache.locks.get(
        ctx_key
    )
    if not lock:
        lock = _Cache.locks[
            ctx_key
        ] = trio.StrictFIFOLock()
        header: str = 'Allocated NEW lock for @acm_func,\n'
    else:
        header: str = 'Reusing OLD lock for @acm_func,\n'

    log.debug(
        f'{header}'
        f'Acquiring..\n'
        f'task={task!r}\n'
        f'ctx_key={ctx_key!r}\n'
        f'acm_func={acm_func}\n'
    )
    await lock.acquire()
    log.debug(
        f'Acquired lock..\n'
        f'task={task!r}\n'
        f'ctx_key={ctx_key!r}\n'
        f'acm_func={acm_func}\n'
    )

    # XXX: one singleton nursery per actor and we want to
    # have it not be closed until all consumers have exited (which is
    # currently difficult to implement any other way besides using our
    # pre-allocated runtime instance..)
    if tn:
        # TODO, assert tn is eventual parent of this task!
        task: trio.Task = trio.lowlevel.current_task()
        task_tn: trio.Nursery = task.parent_nursery
        if not tn._cancel_status.encloses(
            task_tn._cancel_status
        ):
            raise RuntimeError(
                f'Mis-nesting of task under provided {tn} !?\n'
                f'Current task is NOT a child(-ish)!!\n'
                f'\n'
                f'task: {task}\n'
                f'task_tn: {task_tn}\n'
            )
        service_tn = tn
    else:
        service_tn: trio.Nursery = current_actor()._service_tn

    # TODO: is there any way to allocate
    # a 'stays-open-till-last-task-finshed nursery?
    # service_tn: trio.Nursery
    # async with maybe_open_nursery(_Cache.service_tn) as service_tn:
    #     _Cache.service_tn = service_tn

    cache_miss_ke: KeyError|None = None
    maybe_taskc: trio.Cancelled|None = None
    try:
        # **critical section** that should prevent other tasks from
        # checking the _Cache until complete otherwise the scheduler
        # may switch and by accident we create more then one resource.
        yielded = _Cache.values[ctx_key]
        # XXX^ should key-err if not-yet-allocated

    except KeyError as _ke:
        # XXX, stay mutexed up to cache-miss yield
        try:
            cache_miss_ke = _ke
            log.debug(
                f'Allocating new @acm-func entry\n'
                f'ctx_key={ctx_key}\n'
                f'acm_func={acm_func}\n'
            )
            mngr = acm_func(**kwargs)
            resources = _Cache.resources
            entry: tuple|None = resources.get(ctx_key)
            if entry:
                raise RuntimeError(
                    f'Caching resources ALREADY exist?!\n'
                    f'ctx_key={ctx_key!r}\n'
                    f'acm_func={acm_func}\n'
                    f'task: {task}\n'
                )

            ctx_exit = _CtxExit()
            resources[ctx_key] = (
                service_tn,
                trio.Event(),
                ctx_exit,
            )
            try:
                yielded: Any = await service_tn.start(
                    _Cache.run_ctx,
                    mngr,
                    ctx_key,
                    ctx_exit,
                )
            except BaseException:
                # If `run_ctx` (wrapping the acm's `__aenter__`)
                # fails or is cancelled, clean up the `resources`
                # entry we just set — OW it leaks permanently.
                # |_ https://github.com/goodboy/tractor/pull/436#discussion_r3047201323
                resources.pop(ctx_key, None)
                raise
            _Cache.users[ctx_key] += 1
            user_registered = True
        finally:
            # XXX, since this runs from an `except` it's a checkpoint
            # which can be `trio.Cancelled`-masked.
            #
            # NOTE, in that case the mutex is never released by the
            # (first and) caching task and **we can't** simply shield
            # bc that will inf-block on the `await
            # no_more_users.wait()`.
            #
            # SO just always unlock!
            lock.release()

        try:
            yield (
                False,  # cache_hit = "no"
                yielded,
            )
        except trio.Cancelled as taskc:
            maybe_taskc = taskc
            log.cancel(
                f'Cancelled from cache-miss entry\n'
                f'\n'
                f'ctx_key: {ctx_key!r}\n'
                f'mngr: {mngr!r}\n'
            )
            # XXX, always unset ke from cancelled context
            # since we never consider it a masked exc case!
            # - bc this can be called directly ty `._rpc._invoke()`?
            #
            if maybe_taskc.__context__ is cache_miss_ke:
                maybe_taskc.__context__ = None

            raise taskc
    else:
        # XXX, cached-entry-path
        (
            _,
            _,
            ctx_exit,
        ) = _Cache.resources[ctx_key]
        _Cache.users[ctx_key] += 1
        user_registered = True
        log.debug(
            f'Re-using cached resource for user {_Cache.users}\n\n'
            f'{ctx_key!r} -> {type(yielded)}\n'

            # TODO: make this work with values but without
            # `msgspec.Struct` causing frickin crashes on field-type
            # lookups..
            # f'{ctx_key!r} -> {yielded!r}\n'
        )
        lock.release()
        yield (
            True,  # cache_hit = "yes"
            yielded,
        )

    finally:
        if user_registered:
            # Serialize user registration and teardown under the same
            # per-key lock so no entrant can acquire a resource after
            # its final user has committed to exiting it.
            with trio.CancelScope(shield=True):
                await lock.acquire()
                try:
                    _Cache.users[ctx_key] -= 1

                    # If no consumers remain, keep entrants queued
                    # until the cached context has completely exited.
                    if _Cache.users[ctx_key] <= 0:
                        log.debug(
                            f'De-allocating @acm-func entry\n'
                            f'ctx_key={ctx_key!r}\n'
                            f'acm_func={acm_func!r}\n'
                        )

                        # XXX: if we're cancelled, the entry may
                        # have never been entered since the nursery
                        # task was killed.
                        entry = _Cache.resources.get(ctx_key)
                        if entry:
                            (
                                _,
                                no_more_users,
                                ctx_exit,
                            ) = entry
                            no_more_users.set()

                        assert ctx_exit is not None
                        await ctx_exit.done.wait()
                        exit_error = ctx_exit.error

                        # A queued entrant already holds a reference
                        # to this lock. Keep it registered until that
                        # task has acquired and released it.
                        stats = lock.statistics()
                        if not stats.tasks_waiting:
                            maybe_lock = _Cache.locks.get(ctx_key)
                            if maybe_lock is lock:
                                _Cache.locks.pop(ctx_key)
                            else:
                                log.error(
                                    f'Resource lock for {ctx_key} '
                                    f'was replaced before teardown?'
                                )
                finally:
                    lock.release()

        if exit_error is not None:
            # Always re-raise a regular `__aexit__()` error at the
            # final consumer's context boundary.
            raise exit_error
