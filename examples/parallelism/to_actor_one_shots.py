'''
`tractor.to_actor.run()`: one-shot single-task subactor
invocation, the SC-parallelism sibling of
`trio.to_thread.run_sync()` (and `anyio.to_process`).

Each call spawns a subactor, schedules the async fn as
its lone remote task, waits on the result and reaps the
subactor. Concurrency composes the plain `trio` way:
schedule multiple one-shot calls in a local task nursery
against a shared actor-nursery; any remote error raises
directly in the task which scheduled it.

'''
import math

import tractor
import trio


async def is_prime(
    n: int,
) -> bool:
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


async def main() -> None:

    # fully implicit one-shot: boots the actor-runtime,
    # spawns a subactor, runs the task, reaps the
    # subactor, tears the runtime back down.
    assert await tractor.to_actor.run(
        is_prime,
        n=2,
    )

    # the "worker-pool-ish" pattern from the original
    # `concurrent.futures` example: one subactor per
    # input, all concurrent, results and errors
    # collected by caller-side tasks.
    results: dict[int, bool] = {}

    async def check(
        an: tractor.ActorNursery,
        n: int,
        i: int,
    ) -> None:
        results[n] = await tractor.to_actor.run(
            is_prime,
            an=an,
            name=f'prime_checker_{i}',
            n=n,
        )

    inputs: list[int] = [
        7,
        8,
        3691,
        3693,
    ]
    async with (
        tractor.open_nursery() as an,
        trio.open_nursery() as tn,
    ):
        for i, n in enumerate(inputs):
            tn.start_soon(check, an, n, i)

    for n, prime in sorted(results.items()):
        print(f'{n} is prime: {prime}')


if __name__ == '__main__':
    trio.run(main)
