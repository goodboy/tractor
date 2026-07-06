import trio
import tractor


async def name_error():
    getattr(doggypants)  # noqa (on purpose)


async def main():
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
