import trio
import tractor


log = tractor.log.get_logger('multiportal')


async def stream_data(seed=10):
    log.info("Starting stream task")

    for i in range(seed):
        yield i
        await trio.sleep(0)  # trigger scheduler


async def stream_from_portal(p, consumed):

    async for item in await p.run(stream_data):
        if item in consumed:
            consumed.remove(item)
        else:
            consumed.append(item)


async def main():

    async with tractor.open_nursery(loglevel='info') as an:

        p = await an.start_actor('stream_boi', enable_modules=[__name__])

        consumed = []

        async with trio.open_nursery() as n:
            for i in range(2):
                n.start_soon(stream_from_portal, p, consumed)

        # both streaming consumer tasks have completed and so we should
        # have nothing in our list thanks to single threadedness
        assert not consumed

        await an.cancel()


if __name__ == '__main__':
    trio.run(main)
