
import trio
import tractor


async def sleepy_jane() -> None:
    uid: tuple = tractor.current_actor().uid
    print(f'Yo i am actor {uid}')
    await trio.sleep_forever()


async def main():
    '''
    Spawn a flat actor cluster, with one process per detected core.

    '''
    portal_map: dict[str, tractor.Portal]

    # look at this hip new syntax!
    async with (

        tractor.open_actor_cluster(
            modules=[__name__]
        ) as portal_map,

        trio.open_nursery(
            strict_exception_groups=False,
        ) as tn,
    ):

        for (name, portal) in portal_map.items():
            tn.start_soon(
                portal.run,
                sleepy_jane,
            )

        await trio.sleep(0.5)

        # kill the cluster with a cancel
        raise KeyboardInterrupt


if __name__ == '__main__':
    try:
        trio.run(main)
    except KeyboardInterrupt:
        print('trio cancelled by KBI')
