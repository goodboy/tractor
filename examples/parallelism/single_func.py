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

    return os.getpid()


async def main():

    async with tractor.open_nursery() as n:

        portal = await n.run_in_actor(burn_cpu)

        #  burn rubber in the parent too
        await burn_cpu()

        # wait on result from target function
        pid = await portal.result()

    # end of nursery block
    print(f"Collected subproc {pid}")


if __name__ == '__main__':
    trio.run(main)
