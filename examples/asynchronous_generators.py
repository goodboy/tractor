from itertools import repeat
from typing import AsyncIterator

import trio
import tractor


async def stream_forever() -> AsyncIterator[str]:
    '''
    Stream the same message indefinitely.

    '''
    message: str
    for message in repeat(
        'I can see these little future bubble things',
    ):
        # each yielded value is sent over the ``Channel`` to the
        # parent actor
        yield message
        await trio.sleep(0.01)


async def main() -> None:
    '''
    Print messages streamed from a subactor.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:

        portal: tractor.Portal = await an.start_actor(
            'donny',
            enable_modules=[__name__],
        )

        # this async for loop streams values from the above
        # async generator running in a separate process
        stream: tractor.MsgStream
        async with portal.open_stream_from(stream_forever) as stream:
            count: int = 0
            message: str
            async for message in stream:
                print(message)
                count += 1

                if count > 50:
                    break

        print('stream terminated')

        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
