
import trio
import tractor


async def sleepy_jane():
    uid = tractor.current_actor().uid
    print(f'Yo i am actor {uid}')
    await trio.sleep_forever()


async def main():
    '''
    Spawn a flat actor cluster, with one process per
    detected core.

    '''
    portal_map: dict[str, tractor.Portal]
    results: dict[str, str]

    # look at this hip new syntax!
    async with (

        tractor.open_actor_cluster(
            modules=[__name__]
        ) as portal_map,

        trio.open_nursery() as n,
    ):

        for (name, portal) in portal_map.items():
            n.start_soon(portal.run, sleepy_jane)

        await trio.sleep(0.5)

        # kill the cluster with a cancel
        raise KeyboardInterrupt


if __name__ == '__main__':
    try:
        trio.run(main)
    except KeyboardInterrupt:
        pass
