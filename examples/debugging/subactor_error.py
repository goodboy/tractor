import trio
import tractor


async def name_error() -> None:
    '''
    Deliberately raise a ``NameError`` in a subactor.

    '''
    getattr(doggypants)  # noqa (on purpose)


async def main() -> None:
    '''
    Surface a subactor `NameError` at the waiting root task.

    '''
    async with tractor.open_nursery(
        debug_mode=True,
    ) as an:

        # TODO: ideally the REPL arrives at this frame in the parent,
        # ABOVE the @api_frame of `to_actor.run()` ..
        # await tractor.pause()

        # the one-shot blocks on the subactor's result so the
        # boxed `NameError` raises right here.
        await tractor.to_actor.run(name_error, an=an)


if __name__ == '__main__':
    trio.run(main)
