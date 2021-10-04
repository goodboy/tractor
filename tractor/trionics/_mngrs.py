'''
Async context manager primitives with hard ``trio``-aware semantics

'''
from typing import AsyncContextManager
from typing import TypeVar
from contextlib import asynccontextmanager as acm

import trio


# A regular invariant generic type
T = TypeVar("T")


async def _enter_and_sleep(

    mngr: AsyncContextManager[T],
    to_yield: dict[int, T],
    all_entered: trio.Event,
    # task_status: TaskStatus[T] = trio.TASK_STATUS_IGNORED,

) -> T:
    '''Open the async context manager deliver it's value
    to this task's spawner and sleep until cancelled.

    '''
    async with mngr as value:
        to_yield[id(mngr)] = value

        if all(to_yield.values()):
            all_entered.set()

        # sleep until cancelled
        await trio.sleep_forever()


@acm
async def async_enter_all(

    *mngrs: list[AsyncContextManager[T]],

) -> tuple[T]:

    to_yield = {}.fromkeys(id(mngr) for mngr in mngrs)

    all_entered = trio.Event()

    async with trio.open_nursery() as n:
        for mngr in mngrs:
            n.start_soon(
                _enter_and_sleep,
                mngr,
                to_yield,
                all_entered,
            )

        # deliver control once all managers have started up
        await all_entered.wait()
        yield tuple(to_yield.values())

        # tear down all sleeper tasks thus triggering individual
        # mngr ``__aexit__()``s.
        n.cancel_scope.cancel()
