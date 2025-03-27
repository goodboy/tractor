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
    async with tractor.open_nursery() as n:
        if depth < 1:

            await n.run_in_actor(breakpoint_forever)

            p = await n.run_in_actor(
                name_error,
                name='name_error'
            )
            await trio.sleep(0.5)
            # rx and propagate error from child
            await p.result()

        else:
            # recusrive call to spawn another process branching layer of
            # the tree
            depth -= 1
            await n.run_in_actor(
                spawn_until,
                depth=depth,
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
    async with tractor.open_nursery(
        debug_mode=True,
        # loglevel='cancel',
    ) as n:

        # spawn both actors
        portal = await n.run_in_actor(
            spawn_until,
            depth=3,
            name='spawner0',
        )
        portal1 = await n.run_in_actor(
            spawn_until,
            depth=4,
            name='spawner1',
        )

        # TODO: test this case as well where the parent don't see
        # the sub-actor errors by default and instead expect a user
        # ctrl-c to kill the root.
        with trio.move_on_after(3):
            await trio.sleep_forever()

        # gah still an issue here.
        await portal.result()

        # should never get here
        await portal1.result()


if __name__ == '__main__':
    trio.run(main)
