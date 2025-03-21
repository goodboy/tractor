import trio
import tractor

async def die():
    raise RuntimeError


async def main():
    async with tractor.open_nursery() as tn:

        debug_actor = await tn.start_actor(
            'debugged_boi',
            enable_modules=[__name__],
            debug_mode=True,
        )
        crash_boi = await tn.start_actor(
            'crash_boi',
            enable_modules=[__name__],
            # debug_mode=True,
        )

        async with trio.open_nursery() as n:
            n.start_soon(debug_actor.run, die)
            n.start_soon(crash_boi.run, die)


if __name__ == '__main__':
    trio.run(main)
