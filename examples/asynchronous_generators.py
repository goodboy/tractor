from typing import AsyncIterator
from itertools import repeat

import trio
import tractor


async def stream_forever() -> AsyncIterator[int]:

    for i in repeat("I can see these little future bubble things"):
        # each yielded value is sent over the ``Channel`` to the parent actor
        yield i
        await trio.sleep(0.01)


async def main():

    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'donny',
            enable_modules=[__name__],
        )

        # this async for loop streams values from the above
        # async generator running in a separate process
        async with portal.open_stream_from(stream_forever) as stream:
            count = 0
            async for letter in stream:
                print(letter)
                count += 1

                if count > 50:
                    break

        print('stream terminated')

        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
