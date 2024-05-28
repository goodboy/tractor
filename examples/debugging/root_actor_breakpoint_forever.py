import trio
import tractor


async def main(
    registry_addrs: tuple[str, int]|None = None
):

    async with tractor.open_root_actor(
        debug_mode=True,
        # loglevel='runtime',
    ):
        while True:
            await tractor.breakpoint()


if __name__ == '__main__':
    trio.run(main)
