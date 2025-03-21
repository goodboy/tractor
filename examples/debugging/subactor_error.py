import trio
import tractor


async def name_error():
    getattr(doggypants)  # noqa (on purpose)


async def main():
    async with tractor.open_nursery(
        debug_mode=True,
        # loglevel='transport',
    ) as an:

        # TODO: ideally the REPL arrives at this frame in the parent,
        # ABOVE the @api_frame of `Portal.run_in_actor()` (which
        # should eventually not even be a portal method ... XD)
        # await tractor.pause()
        p: tractor.Portal = await an.run_in_actor(name_error)

        # with this style, should raise on this line
        await p.result()

        # with this alt style should raise at `open_nusery()`
        # return await p.result()


if __name__ == '__main__':
    trio.run(main)
