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
    Optional,
    Sequence,
    TypeVar,
    TYPE_CHECKING,
)

import trio
from tractor._state import current_actor
from tractor.log import get_logger
# from ._beg import collapse_eg


if TYPE_CHECKING:
    from tractor import ActorNursery


log = get_logger(__name__)

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

) -> AsyncGenerator[
    tuple[
        T | None,
        ...
    ],
    None,
]:
    '''
    Concurrently enter a sequence of async context managers (`acm`s),
    each scheduled in a separate `trio.Task` and deliver their
    unwrapped `yield`-ed values in the same order once all `@acm`s
    in every task have entered.

    On exit, all `acm`s are subsequently and concurrently exited with
    **no order guarantees**.

    This function is somewhat similar to a batch of non-blocking
    calls to `contextlib.AsyncExitStack.enter_async_context()`
    (inside a loop) *in combo with* a `asyncio.gather()` to get the
    `.__aenter__()`-ed values, except the managers are both
    concurrently entered and exited and *cancellation-just-works™*.

    '''
    seed: int = id(mngrs)
    unwrapped: dict[int, T | None] = {}.fromkeys(
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
            'Use a non-lazy iterator or sequence-type intead!\n'
        )

    async with (
        # collapse_eg(),
        trio.open_nursery(
            strict_exception_groups=False,
            # ^XXX^ TODO? soo roll our own then ??
            # -> since we kinda want the "if only one `.exception` then
            # just raise that" interface?
        ) as tn,
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

        # deliver control once all managers have started up
        await all_entered.wait()

        try:
            yield tuple(unwrapped.values())
        finally:
            # XXX NOTE: this is ABSOLUTELY REQUIRED to avoid
            # the following wacky bug:
            # <tractorbugurlhere>
            parent_exit.set()


# Per actor task caching helpers.
# Further potential examples of interest:
# https://gist.github.com/njsmith/cf6fc0a97f53865f2c671659c88c1798#file-cache-py-L8

class _Cache:
    '''
    Globally (actor-processs scoped) cached, task access to
    a kept-alive-while-in-use async resource.

    '''
    service_n: Optional[trio.Nursery] = None
    locks: dict[Hashable, trio.Lock] = {}
    users: int = 0
    values: dict[Any,  Any] = {}
    resources: dict[
        Hashable,
        tuple[trio.Nursery, trio.Event]
    ] = {}
    # nurseries: dict[int, trio.Nursery] = {}
    no_more_users: Optional[trio.Event] = None

    @classmethod
    async def run_ctx(
        cls,
        mng,
        ctx_key: tuple,
        task_status: trio.TaskStatus[T] = trio.TASK_STATUS_IGNORED,

    ) -> None:
        async with mng as value:
            _, no_more_users = cls.resources[ctx_key]
            cls.values[ctx_key] = value
            task_status.started(value)
            try:
                await no_more_users.wait()
            finally:
                # discard nursery ref so it won't be re-used (an error)?
                value = cls.values.pop(ctx_key)
                cls.resources.pop(ctx_key)


@acm
async def maybe_open_context(

    acm_func: Callable[..., AsyncContextManager[T]],

    # XXX: used as cache key after conversion to tuple
    # and all embedded values must also be hashable
    kwargs: dict = {},
    key: Hashable | Callable[..., Hashable] = None,

) -> AsyncIterator[tuple[bool, T]]:
    '''
    Maybe open an async-context-manager (acm) if there is not already
    a `_Cached` version for the provided (input) `key` for *this* actor.

    Return the `_Cached` instance on a _Cache hit.

    '''
    fid = id(acm_func)

    if inspect.isfunction(key):
        ctx_key = (fid, key(**kwargs))
    else:
        ctx_key = (fid, key or tuple(kwargs.items()))

    # yielded output
    yielded: Any = None
    lock_registered: bool = False

    # Lock resource acquisition around task racing  / ``trio``'s
    # scheduler protocol.
    # NOTE: the lock is target context manager func specific in order
    # to allow re-entrant use cases where one `maybe_open_context()`
    # wrapped factor may want to call into another.
    lock = _Cache.locks.setdefault(fid, trio.Lock())
    lock_registered: bool = True
    await lock.acquire()

    # XXX: one singleton nursery per actor and we want to
    # have it not be closed until all consumers have exited (which is
    # currently difficult to implement any other way besides using our
    # pre-allocated runtime instance..)
    service_n: trio.Nursery = current_actor()._service_n

    # TODO: is there any way to allocate
    # a 'stays-open-till-last-task-finshed nursery?
    # service_n: trio.Nursery
    # async with maybe_open_nursery(_Cache.service_n) as service_n:
    #     _Cache.service_n = service_n

    try:
        # **critical section** that should prevent other tasks from
        # checking the _Cache until complete otherwise the scheduler
        # may switch and by accident we create more then one resource.
        yielded = _Cache.values[ctx_key]

    except KeyError:
        log.debug(f'Allocating new {acm_func} for {ctx_key}')
        mngr = acm_func(**kwargs)
        resources = _Cache.resources
        assert not resources.get(ctx_key), f'Resource exists? {ctx_key}'
        resources[ctx_key] = (service_n, trio.Event())

        # sync up to the mngr's yielded value
        yielded = await service_n.start(
            _Cache.run_ctx,
            mngr,
            ctx_key,
        )
        _Cache.users += 1
        lock.release()
        yield False, yielded

    else:
        _Cache.users += 1
        log.runtime(
            f'Re-using cached resource for user {_Cache.users}\n\n'
            f'{ctx_key!r} -> {type(yielded)}\n'

            # TODO: make this work with values but without
            # `msgspec.Struct` causing frickin crashes on field-type
            # lookups..
            # f'{ctx_key!r} -> {yielded!r}\n'
        )
        lock.release()
        yield True, yielded

    finally:
        _Cache.users -= 1

        if yielded is not None:
            # if no more consumers, teardown the client
            if _Cache.users <= 0:
                log.debug(f'De-allocating resource for {ctx_key}')

                # XXX: if we're cancelled we the entry may have never
                # been entered since the nursery task was killed.
                # _, no_more_users = _Cache.resources[ctx_key]
                entry = _Cache.resources.get(ctx_key)
                if entry:
                    _, no_more_users = entry
                    no_more_users.set()

                if lock_registered:
                    maybe_lock = _Cache.locks.pop(fid, None)
                    if maybe_lock is None:
                        log.error(
                            f'Resource lock for {fid} ALREADY POPPED?'
                        )
