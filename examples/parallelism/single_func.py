'''
Run with a process monitor from a terminal using::

    $TERM -e watch -n 0.1  "pstree -a $$" \
        & python examples/parallelism/single_func.py \
        && kill $!

'''
import os

import tractor
import trio


async def burn_cpu() -> int:
    '''
    Burn CPU briefly and return the current process ID.

    '''
    pid: int = os.getpid()

    # burn a core @ ~ 50kHz
    for _ in range(50000):
        await trio.sleep(1 / 50000 / 50)

    return pid


async def main() -> None:
    '''
    Run ``burn_cpu()`` in the parent and a subactor.

    '''
    async with trio.open_nursery() as tn:

        # burn rubber in the parent too
        tn.start_soon(burn_cpu)

        # run the same func as the lone task in a subactor,
        # block on and collect its PID as the caller-side result
        pid: int = await tractor.to_actor.run(burn_cpu)

    print(f'Collected subproc {pid}')


if __name__ == '__main__':
    trio.run(main)
