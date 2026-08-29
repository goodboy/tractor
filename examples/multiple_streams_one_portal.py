from typing import AsyncIterator

import trio
import tractor


log = tractor.log.get_logger('multiportal')


async def stream_data(seed: int = 10) -> AsyncIterator[int]:
    '''
    Stream a finite sequence of integers.

    '''
    log.info('Starting stream task')

    i: int
    for i in range(seed):
        yield i
        await trio.sleep(0)  # trigger scheduler


async def stream_from_portal(
    portal: tractor.Portal,
    consumed: list[int],
) -> None:
    '''
    Consume one stream and toggle each value in a shared list.

    '''
    stream: tractor.MsgStream
    async with portal.open_stream_from(stream_data) as stream:
        item: int
        async for item in stream:
            if item in consumed:
                consumed.remove(item)
            else:
                consumed.append(item)


async def main() -> None:
    '''
    Consume two concurrent streams through one portal.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery(loglevel='info') as an:

        portal: tractor.Portal = await an.start_actor(
            'stream_boi',
            enable_modules=[__name__],
        )

        consumed: list[int] = []

        n: trio.Nursery
        async with trio.open_nursery() as n:
            for _ in range(2):
                n.start_soon(
                    stream_from_portal,
                    portal,
                    consumed,
                )

        # both streaming consumer tasks have completed and so we
        # should have nothing in our list thanks to single
        # threadedness
        assert not consumed

        await an.cancel()


if __name__ == '__main__':
    trio.run(main)
