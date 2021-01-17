"""
Demonstration of the prime number detector example from the
``concurrent.futures`` docs:

https://docs.python.org/3/library/concurrent.futures.html#processpoolexecutor-example

This uses no extra threads or fancy semaphores besides ``tractor``'s
(TCP) channels.

"""
from contextlib import asynccontextmanager
from typing import List, Callable
import itertools
import math
import time

import tractor
import trio


PRIMES = [
    112272535095293,
    112582705942171,
    112272535095293,
    115280095190773,
    115797848077099,
    1099726899285419]


def is_prime(n):
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


@asynccontextmanager
async def worker_pool(workers=4):
    """Though it's a trivial special case for ``tractor``, the well
    known "worker pool" seems to be the defacto "I want this process
    pattern" for most parallelism pilgrims.

    """

    async with tractor.open_nursery() as tn:

        portals = []
        results = []

        for i in range(workers):

            # this starts a new sub-actor (process + trio runtime) and
            # stores it's "portal" for later use to "submit jobs" (ugh).
            portals.append(
                await tn.start_actor(
                    f'worker_{i}',
                    rpc_module_paths=[__name__],
                )
            )

        async def map(
            worker_func: Callable[[int], bool],
            sequence: List[int]
        ) -> List[bool]:

            # define an async (local) task to collect results from workers
            async def collect_portal_result(func, value, portal):

                results.append((value, await portal.run(func, n=value)))

            async with trio.open_nursery() as n:

                for value, portal in zip(sequence, itertools.cycle(portals)):

                    n.start_soon(
                        collect_portal_result,
                        worker_func,
                        value,
                        portal
                    )

            return results

        yield map

        # tear down all "workers"
        await tn.cancel()


async def main():
    async with worker_pool() as actor_map:

        start = time.time()
        # for number, prime in zip(PRIMES, executor.map(is_prime, PRIMES)):
        for number, prime in await actor_map(is_prime, PRIMES):
            print(f'{number} is prime: {prime}')

        print(f'processing took {time.time() - start} seconds')

if __name__ == '__main__':
    start = time.time()
    tractor.run(
        main,
        loglevel='ERROR',

        # uncomment to use ``multiprocessing`` fork server backend
        # which gives a startup time boost at the expense of nested
        # processs scalability
        # start_method='forkserver')
    )
    print(f'script took {time.time() - start} seconds')
