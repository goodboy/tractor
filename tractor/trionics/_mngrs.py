'''
Async context manager primitives with hard ``trio``-aware semantics

'''
from typing import AsyncContextManager
from typing import TypeVar
from contextlib import asynccontextmanager as acm

import trio


# A regular invariant generic type
T = TypeVar("T")


async def _enter_and_wait(

    mngr: AsyncContextManager[T],
    to_yield: dict[int, T],
    all_entered: trio.Event,
    teardown_trigger: trio.Event,

) -> T:
    '''Open the async context manager deliver it's value
    to this task's spawner and sleep until cancelled.

    '''
    async with mngr as value:
        to_yield[id(mngr)] = value

        if all(to_yield.values()):
            all_entered.set()

        await teardown_trigger.wait()


@acm
async def async_enter_all(

    *mngrs: tuple[AsyncContextManager[T]],
    teardown_trigger: trio.Event,

) -> tuple[T]:

    to_yield = {}.fromkeys(id(mngr) for mngr in mngrs)

    all_entered = trio.Event()

    async with trio.open_nursery() as n:
        for mngr in mngrs:
            n.start_soon(
                _enter_and_wait,
                mngr,
                to_yield,
                all_entered,
                teardown_trigger,
            )

        # deliver control once all managers have started up
        await all_entered.wait()
        yield tuple(to_yield.values())
