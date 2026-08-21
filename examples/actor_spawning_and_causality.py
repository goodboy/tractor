import trio
import tractor


async def cellar_door():
    assert not tractor.is_root_process()
    return "Dang that's beautiful"


async def main():
    """The main ``tractor`` routine.
    """
    # spawn a subactor, run ``cellar_door()`` as its lone task,
    # block until its result arrives and the subactor is reaped.
    print(
        await tractor.to_actor.run(
            cellar_door,
            name='some_linguist',
        )
    )


if __name__ == '__main__':
    trio.run(main)
