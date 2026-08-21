"""
Run with a process monitor from a terminal using::

    $TERM -e watch -n 0.1  "pstree -a $$" \
        & python examples/parallelism/single_func.py \
        && kill $!

"""
import os

import tractor
import trio


async def burn_cpu():

    pid = os.getpid()

    # burn a core @ ~ 50kHz
    for _ in range(50000):
        await trio.sleep(1/50000/50)

    return pid


async def main():

    async with trio.open_nursery() as tn:

        # burn rubber in the parent too
        tn.start_soon(burn_cpu)

        # run the same func as the lone task in a subactor,
        # block on (and collect) its result
        pid = await tractor.to_actor.run(burn_cpu)

    print(f"Collected subproc {pid}")


if __name__ == '__main__':
    trio.run(main)
