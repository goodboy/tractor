from functools import partial

import trio
import tractor


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)  # noqa


async def breakpoint_forever():
    "Indefinitely re-enter debugger in child actor."
    while True:
        await tractor.pause()

        # NOTE: if the test never sent 'q'/'quit' commands
        # on the pdb repl, without this checkpoint line the
        # repl would spin in this actor forever.
        # await trio.sleep(0)


async def spawn_until(depth=0):
    """"A nested nursery that triggers another ``NameError``.
    """
    async with (
        tractor.open_nursery() as an,
        trio.open_nursery() as tn,
    ):
        if depth < 1:

            tn.start_soon(
                partial(
                    tractor.to_actor.run,
                    breakpoint_forever,
                    an=an,
                )
            )

            # Let the background one-shot enter `breakpoint_forever()`
            # before its sibling raises and cancellation propagates.
            await trio.sleep(0.5)
            # rx and propagate error from child
            await tractor.to_actor.run(
                name_error,
                an=an,
                name='name_error',
            )

        else:
            # recusrive call to spawn another process branching layer of
            # the tree; blocks (up) each level until the leaf's
            # `name_error` relays through.
            depth -= 1
            await tractor.to_actor.run(
                partial(
                    spawn_until,
                    depth=depth,
                ),
                an=an,
                name=f'spawn_until_{depth}',
            )


# TODO: notes on the new boxed-relayed errors through proxy actors
async def main():
    """The main ``tractor`` routine.

    The process tree should look as approximately as follows when the debugger
    first engages:

    python examples/debugging/multi_nested_subactors_bp_forever.py
    ├─ python -m tractor._child --uid ('spawner1', '7eab8462 ...)
    │  └─ python -m tractor._child --uid ('spawn_until_3', 'afcba7a8 ...)
    │     └─ python -m tractor._child --uid ('spawn_until_2', 'd2433d13 ...)
    │        └─ python -m tractor._child --uid ('spawn_until_1', '1df589de ...)
    │           └─ python -m tractor._child --uid ('spawn_until_0', '3720602b ...)
    │
    └─ python -m tractor._child --uid ('spawner0', '1d42012b ...)
       └─ python -m tractor._child --uid ('spawn_until_2', '2877e155 ...)
          └─ python -m tractor._child --uid ('spawn_until_1', '0502d786 ...)
             └─ python -m tractor._child --uid ('spawn_until_0', 'de918e6d ...)

    """
    async with (
        tractor.open_nursery(
            debug_mode=True,
            loglevel='pdb',
        ) as an,
        trio.open_nursery() as tn,
    ):
        # spawn both spawner trees as concurrent one-shots; the
        # first tree's (relayed) error cancels the other.
        tn.start_soon(
            partial(
                tractor.to_actor.run,
                partial(
                    spawn_until,
                    depth=3,
                ),
                an=an,
                name='spawner0',
            )
        )
        tn.start_soon(
            partial(
                tractor.to_actor.run,
                partial(
                    spawn_until,
                    depth=4,
                ),
                an=an,
                name='spawner1',
            )
        )


if __name__ == '__main__':
    trio.run(main)
