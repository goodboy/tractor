"""
Run with a process monitor from a terminal using:
$TERM -e watch -n 0.1  "pstree -a $$" & python examples/parallelism/we_are_processes.py && kill $!

"""
from multiprocessing import cpu_count
import os

import tractor
import trio


async def target():
    print(f"Yo, i'm '{tractor.current_actor().name}' "
          f"running in pid {os.getpid()}")
    await trio.sleep_forever()


async def main():

    async with tractor.open_nursery() as n:

        for i in range(cpu_count()):
            await n.run_in_actor(target, name=f'worker_{i}')

        print('This process tree will self-destruct in 1 sec...')
        await trio.sleep(1)

        # you could have done this yourself
        raise Exception('Self Destructed')


if __name__ == '__main__':
    try:
        trio.run(main)
    except Exception:
        print('Zombies Contained')
