import trio
import tractor


async def cellar_door() -> str:
    assert not tractor.is_root_process()
    return "Dang that's beautiful"


async def main() -> None:
    """The main ``tractor`` routine.
    """
    n: tractor.ActorNursery
    async with tractor.open_nursery() as n:

        portal: tractor.Portal = await n.run_in_actor(
            cellar_door,
            name='some_linguist',
        )

    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.

    print(await portal.wait_for_result())


if __name__ == '__main__':
    trio.run(main)
