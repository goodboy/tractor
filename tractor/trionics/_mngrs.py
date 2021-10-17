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
    teardown_trigger: trio.Event,
) -> None:
    '''Open the async context manager deliver it's value
    to this task's spawner and sleep until cancelled.

    '''
    async with mngr as value:
        unwrapped[id(mngr)] = value

        if all(unwrapped.values()):
            all_entered.set()

        await teardown_trigger.wait()


@acm
async def async_enter_all(
    mngrs: Sequence[AsyncContextManager[T]],
    teardown_trigger: trio.Event,
) -> AsyncGenerator[tuple[T, ...], None]:
    """This async context manager expects a 'teardown_trigger' from the
    outside world which will be used internally to gracefully teardown
    individual context managers.
    """
    unwrapped = {}.fromkeys(id(mngr) for mngr in mngrs)

    all_entered = trio.Event()

    async with trio.open_nursery() as n:
        for mngr in mngrs:
            n.start_soon(
                _enter_and_wait,
                mngr,
                unwrapped,
                all_entered,
                teardown_trigger,
            )

        # deliver control once all managers have started up
        await all_entered.wait()

        yield tuple(unwrapped.values())
