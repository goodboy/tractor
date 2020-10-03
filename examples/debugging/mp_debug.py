import tractor
import trio


async def bubble():
    print('IN BUBBLE')
    while True:
        await trio.sleep(.1)
        await tractor.breakpoint()


async def name_error():
    getattr(doggy)


async def main():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:

        portal1 = await n.run_in_actor('bubble', bubble)
        portal = await n.run_in_actor('name_error', name_error)
        await portal1.result()
        await portal.result()

    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.


# TODO:
# - recurrent entry from single actor
# - recurrent entry to breakpoint() from single actor *after* and an
#   error
# - root error alongside child errors
# - recurrent root errors


if __name__ == '__main__':
    tractor.run(main, loglevel='info', debug_mode=True)
