import tractor


def cellar_door():
   return "Dang that's beautiful"


async def main():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:

        portal = await n.run_in_actor('some_linguist', cellar_door)

    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.

    print(await portal.result())


if __name__ == '__main__':
    tractor.run(main)
