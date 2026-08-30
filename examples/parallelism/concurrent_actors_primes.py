'''
Demonstration of the prime number detector example from the
``concurrent.futures`` docs:

https://docs.python.org/3/library/concurrent.futures.html\
#processpoolexecutor-example

This uses no extra threads, fancy semaphores or futures; all we need
is ``tractor``'s channels.

'''
from contextlib import (
    asynccontextmanager as acm,
    aclosing,
)
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
)
import itertools
import math
import time

import tractor
import trio


type ActorMap = Callable[
    [Callable[[int], Awaitable[bool]], list[int]],
    AsyncIterator[tuple[int, bool]],
]

PRIMES: list[int] = [
    112272535095293,
    112582705942171,
    112272535095293,
    115280095190773,
    115797848077099,
    1099726899285419,
]


async def is_prime(n: int) -> bool:
    '''
    Return whether ``n`` is prime.

    '''
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False

    sqrt_n = int(math.floor(math.sqrt(n)))
    for i in range(3, sqrt_n + 1, 2):
        if n % i == 0:
            return False
    return True


@acm
async def worker_pool(
    workers: int = 4,
) -> AsyncIterator[ActorMap]:
    '''
    Though it's a trivial special case for ``tractor``, the well
    known "worker pool" seems to be the defacto "but, I want this
    process pattern!" for most parallelism pilgrims.

    Yes, the workers stay alive (and ready for work) until you close
    the context.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:

        portals: list[tractor.Portal] = []
        snd_chan: trio.MemorySendChannel[tuple[int, bool]]
        recv_chan: trio.MemoryReceiveChannel[tuple[int, bool]]
        snd_chan, recv_chan = trio.open_memory_channel(len(PRIMES))

        i: int
        for i in range(workers):

            # this starts a new sub-actor (process + trio
            # runtime) and stores it's "portal" for later use to
            # "submit jobs" (ugh).
            portals.append(
                await an.start_actor(
                    f'worker_{i}',
                    enable_modules=[__name__],
                )
            )

        async def _map(
            worker_func: Callable[[int], Awaitable[bool]],
            sequence: list[int],
        ) -> AsyncIterator[tuple[int, bool]]:
            '''
            Dispatch values across workers and yield their results.

            '''
            # define an async (local) task to collect results from
            # workers
            async def send_result(
                func: Callable[[int], Awaitable[bool]],
                value: int,
                portal: tractor.Portal,
            ) -> None:
                '''
                Run one remote worker call and send its result.

                '''
                result: bool = await portal.run(func, n=value)
                await snd_chan.send((value, result))

            tn: trio.Nursery
            async with trio.open_nursery() as tn:

                value: int
                portal: tractor.Portal
                for value, portal in zip(
                    sequence,
                    itertools.cycle(portals),
                ):
                    tn.start_soon(
                        send_result,
                        worker_func,
                        value,
                        portal
                    )

                # deliver results as they arrive
                for _ in range(len(sequence)):
                    yield await recv_chan.receive()

        # deliver the parallel "worker mapper" to user code
        yield _map

        # tear down all "workers" on pool close
        await an.cancel()


async def main() -> None:
    '''
    Report primality results from a pool of actors.

    '''
    actor_map: ActorMap
    async with worker_pool() as actor_map:

        start: float = time.time()

        results: AsyncIterator[tuple[int, bool]]
        async with aclosing(actor_map(is_prime, PRIMES)) as results:
            number: int
            prime: bool
            async for number, prime in results:

                print(f'{number} is prime: {prime}')

        elapsed: float = time.time() - start
        print(f'processing took {elapsed} seconds')


if __name__ == '__main__':
    start: float = time.time()
    trio.run(main)
    elapsed: float = time.time() - start
    print(f'script took {elapsed} seconds')
