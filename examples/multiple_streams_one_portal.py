import trio
import tractor


log = tractor.log.get_logger('multiportal')


async def stream_data(seed: int = 10):
    log.info("Starting stream task")

    for i in range(seed):
        yield i
        await trio.sleep(0)  # trigger scheduler


async def stream_from_portal(
    p: tractor.Portal,
    consumed: list,
) -> None:

    async with p.open_stream_from(stream_data) as stream:
        async for item in stream:
            if item in consumed:
                consumed.remove(item)
            else:
                consumed.append(item)


async def main() -> None:

    an: tractor.ActorNursery
    async with tractor.open_nursery(loglevel='info') as an:

        p: tractor.Portal = await an.start_actor(
            'stream_boi',
            enable_modules=[__name__],
        )

        consumed: list = []

        n: trio.Nursery
        async with trio.open_nursery() as n:
            for i in range(2):
                n.start_soon(stream_from_portal, p, consumed)

        # both streaming consumer tasks have completed and so we should
        # have nothing in our list thanks to single threadedness
        assert not consumed

        await an.cancel()


if __name__ == '__main__':
    trio.run(main)
