import trio
import tractor


async def breakpoint_forever():
    """Indefinitely re-enter debugger in child actor.
    """
    while True:
        await trio.sleep(0.1)
        await tractor.breakpoint()


async def main():

    async with tractor.open_nursery() as n:

        portal = await n.run_in_actor(
            'breakpoint_forever',
            breakpoint_forever,
        )
        await portal.result()


if __name__ == '__main__':
    tractor.run(main, debug_mode=True)
