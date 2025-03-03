import tractor
import trio


async def breakpoint_forever():
    "Indefinitely re-enter debugger in child actor."
    try:
        while True:
            yield 'yo'
            await tractor.pause()
    except BaseException:
        tractor.log.get_console_log().exception(
            'Cancelled while trying to enter pause point!'
        )
        raise


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)  # noqa


async def main():
    '''
    Test breakpoint in a streaming actor.

    '''
    async with tractor.open_nursery(
        debug_mode=True,
        loglevel='cancel',
        # loglevel='devx',
    ) as n:

        p0 = await n.start_actor('bp_forever', enable_modules=[__name__])
        p1 = await n.start_actor('name_error', enable_modules=[__name__])

        # retreive results
        async with p0.open_stream_from(breakpoint_forever) as stream:

            # triggers the first name error
            try:
                await p1.run(name_error)
            except tractor.RemoteActorError as rae:
                assert rae.boxed_type is NameError

            async for i in stream:

                # a second time try the failing subactor and this tie
                # let error propagate up to the parent/nursery.
                await p1.run(name_error)


if __name__ == '__main__':
    trio.run(main)
