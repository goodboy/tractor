from functools import partial

import trio
import tractor


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)  # noqa


async def spawn_until(depth=0):
    """"A nested nursery that triggers another ``NameError``.
    """
    async with tractor.open_nursery() as an:
        if depth < 1:
            await tractor.to_actor.run(name_error, an=an)
        else:
            depth -= 1
            await tractor.to_actor.run(
                partial(
                    spawn_until,
                    depth=depth,
                ),
                an=an,
                name=f'spawn_until_{depth}',
            )


async def main():
    '''
    The process tree should look as approximately as follows when the
    debugger first engages:

    python examples/debugging/multi_nested_subactors_bp_forever.py
    ├─ python -m tractor._child --uid ('spawner1', '7eab8462 ...)
    │  └─ python -m tractor._child --uid ('spawn_until_0', '3720602b ...)
    │     └─ python -m tractor._child --uid ('name_error', '505bf71d ...)
    │
    └─ python -m tractor._child --uid ('spawner0', '1d42012b ...)
       └─ python -m tractor._child --uid ('name_error', '6c2733b8 ...)

    '''
    async with (
        tractor.open_nursery(
            debug_mode=True,
            enable_transports=['uds'],  # TODO, apss this via osenv?
            loglevel='devx',  # XXX, required for test!
        ) as an,
        trio.open_nursery() as tn,
    ):
        # spawn the deeper tree in the bg..
        tn.start_soon(
            partial(
                tractor.to_actor.run,
                partial(
                    spawn_until,
                    depth=1,
                ),
                an=an,
                name='spawner1',
            )
        )

        # ..while blocking on the shallow (faster to fail) tree
        # whose propagated error triggers nursery cancellation.
        await tractor.to_actor.run(
            partial(
                spawn_until,
                depth=0,
            ),
            an=an,
            name='spawner0',
        )


if __name__ == '__main__':
    trio.run(main)
