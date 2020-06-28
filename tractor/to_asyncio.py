"""
Infection apis for ``asyncio`` loops running ``trio`` using guest mode.
"""
import asyncio
import inspect
from typing import (
    Any,
    Callable,
    AsyncGenerator,
    Awaitable,
    Union,
)

import trio


async def _invoke(
    from_trio,
    to_trio,
    coro
) -> Union[AsyncGenerator, Awaitable]:
    """Await or stream awaiable object based on type into
    ``trio`` memory channel.
    """
    async def stream_from_gen(c):
        async for item in c:
            to_trio.put_nowait(item)
        to_trio.put_nowait

    async def just_return(c):
        to_trio.put_nowait(await c)

    if inspect.isasyncgen(coro):
        return await stream_from_gen(coro)
    elif inspect.iscoroutine(coro):
        return await coro


# TODO: make this some kind of tractor.to_asyncio.run()
async def run(
    func: Callable,
    qsize: int = 2**10,
    **kwargs,
) -> Any:
    """Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.
    """
    # ITC (inter task comms)
    from_trio = asyncio.Queue(qsize)
    to_trio, from_aio = trio.open_memory_channel(qsize)

    # allow target func to accept/stream results manually
    kwargs['to_trio'] = to_trio
    kwargs['from_trio'] = to_trio

    coro = func(**kwargs)

    # start the asyncio task we submitted from trio
    # TODO: try out ``anyio`` asyncio based tg here
    asyncio.create_task(_invoke(from_trio, to_trio, coro))

    # determine return type async func vs. gen
    if inspect.isasyncgen(coro):
        await from_aio.get()
    elif inspect.iscoroutine(coro):
        async def gen():
            async for tick in from_aio:
                yield tuple(tick)

        return gen()
