import tractor
import trio


async def breakpoint_forever():
    "Indefinitely re-enter debugger in child actor."
    while True:
        await trio.sleep(0.1)
        await tractor.breakpoint()


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)


async def spawn_error():
    """"A nested nursery that triggers another ``NameError``.
    """
    async with tractor.open_nursery() as n:
        portal = await n.run_in_actor('name_error_1', name_error)
        return await portal.result()


async def main():
    """The main ``tractor`` routine.

    The process tree should look as approximately as follows:

    -python examples/debugging/multi_subactors.py
    |-python -m tractor._child --uid ('name_error', 'a7caf490 ...)
    |-python -m tractor._child --uid ('bp_forever', '1f787a7e ...)
    `-python -m tractor._child --uid ('spawn_error', '52ee14a5 ...)
       `-python -m tractor._child --uid ('name_error', '3391222c ...)
    """
    async with tractor.open_nursery() as n:

        # spawn both actors
        portal1 = await n.run_in_actor('bp_forever', breakpoint_forever)
        portal = await n.run_in_actor('name_error', name_error)
        portal2 = await n.run_in_actor('spawn_error', spawn_error)

        # attempt to collect results (which raises error in parent)
        # still has some issues where the parent seems to get stuck
        # await portal.result()
        # await portal1.result()
        # await portal2.result()


if __name__ == '__main__':
    tractor.run(main, debug_mode=True)
