'''
Run with a process monitor from a terminal using::

    $TERM -e watch -n 0.1  "pstree -a $$" \
        & python examples/parallelism/we_are_processes.py \
        && kill $!

'''
from multiprocessing import cpu_count
import os

import tractor
import trio


@tractor.context
async def endpoint(
    ctx: tractor.Context,
):
    actor_name: str = tractor.current_actor().name
    pid: int = os.getpid()
    await ctx.started((actor_name, pid))
    await trio.sleep_forever()


async def open_ep(
    ptl: tractor.Portal,
    i: int,
) -> None:
    ctx: tractor.Context
    async with ptl.open_context(endpoint) as (
        ctx,
        (sub_name, sub_pid),
    ):
        print(
            f'Started ep-task in subactor,\n'
            f'{i}::{sub_name!r}@{sub_pid}\n'
        )
        await ctx.wait_for_result()


async def spawn_and_open_ep(
    an: tractor.ActorNursery,
    i: int,
    maybe_ptl: tractor.Portal|None = None,
) -> None:
    '''
    Spawn a subactor, start a remote `endpoint()`-task in it.

    '''
    if maybe_ptl is None:
        maybe_ptl: tractor.Portal = await an.start_actor(
            name=f'worker_{i}',
            enable_modules=[__name__],
        )
    await open_ep(
        ptl=maybe_ptl,
        i=i,
    )


async def main(
    # spawn subs concurrently (in bg `trio.Task`s) so each
    # actor's cold `import tractor` (~0.4s, see #470) overlaps
    # instead of stacking; once forkserver (#463) lands, spawn
    # is cheap enough to just loop sequentially.
    spawn_subs_in_bg_tasks: bool = True,
):
    '''
    Spawn a subactor-per-CPU then self-destruct the cluster.

    '''
    tn: trio.Nursery
    an: tractor.ActorNursery
    async with (
        tractor.open_nursery(
            # XXX coming soon!
            # https://github.com/goodboy/tractor/pull/463
            # start_method='main_thread_forkserver',
        ) as an,
        trio.open_nursery() as tn,
    ):
        for i in range(cpu_count()):

            maybe_ptl: tractor.Portal|None = None
            if not spawn_subs_in_bg_tasks:
                maybe_ptl: tractor.Portal = await an.start_actor(
                    name=f'worker_{i}',
                    enable_modules=[__name__],
                )
            tn.start_soon(
                spawn_and_open_ep,
                an,
                i,
                maybe_ptl,
            )
        destruct_in: int = 2
        print(
            f'This tree will self-destruct in {destruct_in}s..\n'
        )
        await trio.sleep(destruct_in)
        raise Exception('Self Destructed')


if __name__ == '__main__':
    try:
        trio.run(main)
    except Exception:
        print('Zombies Contained')
