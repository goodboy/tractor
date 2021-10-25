'''
Async context manager primitives with hard ``trio``-aware semantics

'''
from typing import AsyncContextManager, AsyncGenerator
from typing import TypeVar, Sequence
from contextlib import asynccontextmanager as acm

import trio


# A regular invariant generic type
T = TypeVar("T")


async def _enter_and_wait(

    mngr: AsyncContextManager[T],
    unwrapped: dict[int, T],
    all_entered: trio.Event,
    parent_exit: trio.Event,

) -> None:
    '''
    Open the async context manager deliver it's value
    to this task's spawner and sleep until cancelled.

    '''
    async with mngr as value:
        unwrapped[id(mngr)] = value

        if all(unwrapped.values()):
            all_entered.set()

        await parent_exit.wait()


@acm
async def gather_contexts(

    mngrs: Sequence[AsyncContextManager[T]],

) -> AsyncGenerator[tuple[T, ...], None]:
    '''
    Concurrently enter a sequence of async context managers, each in
    a separate ``trio`` task and deliver the unwrapped values in the
    same order once all managers have entered. On exit all contexts are
    subsequently and concurrently exited.

    This function is somewhat similar to common usage of
    ``contextlib.AsyncExitStack.enter_async_context()`` (in a loop) in
    combo with ``asyncio.gather()`` except the managers are concurrently
    entered and exited cancellation just works.

    '''
    unwrapped = {}.fromkeys(id(mngr) for mngr in mngrs)

    all_entered = trio.Event()
    parent_exit = trio.Event()

    async with trio.open_nursery() as n:
        for mngr in mngrs:
            n.start_soon(
                _enter_and_wait,
                mngr,
                unwrapped,
                all_entered,
                parent_exit,
            )

        # deliver control once all managers have started up
        await all_entered.wait()

        yield tuple(unwrapped.values())

        # we don't need a try/finally since cancellation will be triggered
        # by the surrounding nursery on error.
        parent_exit.set()
