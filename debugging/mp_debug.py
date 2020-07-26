import tractor
import trio


async def bubble():
    print('IN BUBBLE')
    await trio.sleep(.1)
    await tractor.breakpoint()


async def bail():
    getattr(doggy)


async def main():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:

        portal1 = await n.run_in_actor('bubble', bubble)
        portal = await n.run_in_actor('bail', bail)
        # await portal.result()
        # await portal1.result()

    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.


if __name__ == '__main__':
    tractor.run(main, loglevel='critical', debug_mode=True)
