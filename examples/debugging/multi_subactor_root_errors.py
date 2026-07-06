'''
Test that a nested nursery will avoid clobbering
the debugger latched by a broken child.

'''
import trio
import tractor


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)  # noqa


async def spawn_error():
    """"A nested nursery that triggers another ``NameError``.
    """
    async with tractor.open_nursery() as n:
        return await tractor.to_actor.run(
            name_error,
            an=n,
            name='name_error_1',
        )


async def main():
    """The main ``tractor`` routine.

    The process tree should look as approximately as follows:

    python examples/debugging/multi_subactors.py
    ├─ python -m tractor._child --uid ('name_error', 'a7caf490 ...)
    `-python -m tractor._child --uid ('spawn_error', '52ee14a5 ...)
       `-python -m tractor._child --uid ('name_error', '3391222c ...)

    Order of failure:
        - nested name_error sub-sub-actor
        - root actor should then fail on assert
        - program termination
    """
    async with (
        tractor.open_nursery(
            debug_mode=True,
            loglevel='devx',
        ) as an,
        trio.open_nursery() as tn,
    ):
        # spawn both actors..
        portal = await an.start_actor(
            'name_error',
            enable_modules=[__name__],
        )
        portal1 = await an.start_actor(
            'spawn_error',
            enable_modules=[__name__],
        )

        # ..and bg-schedule their erroring tasks.
        tn.start_soon(portal.run, name_error)
        tn.start_soon(portal1.run, spawn_error)

        # yield to the bg tasks so both RPC requests are
        # submitted (and start crashing) before the root's own
        # error below (the legacy `run_in_actor()` submitted
        # in-line with each spawn).
        await trio.sleep(0.5)

        # trigger a root actor error
        assert 0


if __name__ == '__main__':
    trio.run(main)
