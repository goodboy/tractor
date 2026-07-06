import tractor
import trio


async def breakpoint_forever():
    "Indefinitely re-enter debugger in child actor."
    while True:
        await trio.sleep(0.1)
        await tractor.pause()


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

    -python examples/debugging/multi_subactors.py
    |-python -m tractor._child --uid ('name_error', 'a7caf490 ...)
    |-python -m tractor._child --uid ('bp_forever', '1f787a7e ...)
    `-python -m tractor._child --uid ('spawn_error', '52ee14a5 ...)
       `-python -m tractor._child --uid ('name_error', '3391222c ...)
    """
    errors: list[BaseException] = []

    async with tractor.open_nursery(
        debug_mode=True,
        # loglevel='runtime',
    ) as an:

        async def run_and_collect(fn):
            '''
            One-shot whose (boxed) error is stashed instead of
            raised so a sibling's crash never cancels the others
            before they've had their own debugger sessions (the
            "collect all errors" the legacy `run_in_actor()` API
            did implicitly at nursery teardown).

            '''
            try:
                await tractor.to_actor.run(fn, an=an)
            except tractor.RemoteActorError as rae:
                errors.append(rae)

        # Spawn all one-shot task actors, collecting (vs.
        # raising) their errors.
        async with trio.open_nursery() as tn:
            tn.start_soon(run_and_collect, breakpoint_forever)
            tn.start_soon(run_and_collect, name_error)
            tn.start_soon(run_and_collect, spawn_error)

        if errors:
            raise BaseExceptionGroup(
                'multi_subactors errored!',
                errors,
            )


if __name__ == '__main__':
    trio.run(main)
