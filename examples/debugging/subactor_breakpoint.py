import trio
import tractor


async def breakpoint_forever():
    '''
    Indefinitely re-enter debugger in child actor.

    '''
    while True:
        await trio.sleep(0.1)
        await tractor.pause()


async def main():

    async with tractor.open_nursery(
        debug_mode=True,
        loglevel='cancel',
    ) as n:

        # parks awaiting a result which only arrives once the
        # user quits (`BdbQuit`s) the child's REPL loop.
        await tractor.to_actor.run(
            breakpoint_forever,
            an=n,
        )


if __name__ == '__main__':
    trio.run(main)
