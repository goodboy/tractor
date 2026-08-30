import trio
import tractor


async def die() -> None:
    '''
    Deliberately crash the calling actor.

    '''
    raise RuntimeError


async def main() -> None:
    '''
    Crash actors with different debugger settings concurrently.

    '''
    async with tractor.open_nursery() as an:

        debug_actor: tractor.Portal = await an.start_actor(
            'debugged_boi',
            enable_modules=[__name__],
            debug_mode=True,
        )
        crash_boi: tractor.Portal = await an.start_actor(
            'crash_boi',
            enable_modules=[__name__],
            # debug_mode=True,
        )

        async with trio.open_nursery() as n:
            n.start_soon(debug_actor.run, die)
            n.start_soon(crash_boi.run, die)


if __name__ == '__main__':
    trio.run(main)
