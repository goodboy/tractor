'''
``async with ():`` inlined context-stream cancellation testing.

Verify the we raise errors when streams are opened prior to sync-opening
a ``tractor.Context`` beforehand.

'''
from itertools import count

import pytest
import trio
import tractor


@tractor.context
async def really_started(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    try:
        await ctx.started()
    except RuntimeError:
        raise


def test_started_called_more_then_once():

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'too_much_starteds',
                enable_modules=[__name__],
            )

            async with portal.open_context(really_started) as (ctx, sent):
                await trio.sleep(1)
                # pass

    with pytest.raises(tractor.RemoteActorError):
        trio.run(main)


@tractor.context
async def never_open_stream(

    ctx:  tractor.Context,

) -> None:
    '''
    Context which never opens a stream and blocks.

    '''
    await ctx.started()
    await trio.sleep_forever()


@tractor.context
async def keep_sending_from_callee(

    ctx:  tractor.Context,

) -> None:
    '''
    Send endlessly on the calleee stream.

    '''
    await ctx.started()
    async with ctx.open_stream() as stream:
        for msg in count():
            await stream.send(msg)
            await trio.sleep(0.01)


@pytest.mark.parametrize(
    'overrun_by',
    [
        (None, 0, never_open_stream),  # use default settings
        ('caller', 1, never_open_stream),
        ('callee', 0, keep_sending_from_callee),
    ],
    ids='overrun_condition_by={}'.format,
)
def test_one_end_stream_not_opened(overrun_by):
    '''
    This should exemplify the bug from:
    https://github.com/goodboy/tractor/issues/265

    '''
    overrunner, buf_size_increase, entrypoint = overrun_by
    from tractor._actor import Actor
    buf_size = buf_size_increase + Actor.msg_buffer_size

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'starts_no_stream',
                enable_modules=[__name__],
            )

            async with portal.open_context(
                entrypoint,
            ) as (ctx, sent):
                assert sent is None

                if overrunner in (None, 'caller'):

                    async with ctx.open_stream() as stream:
                        for i in range(buf_size - 1):
                            await stream.send(i)

                        if overrunner is None:
                            # without this we block waiting on the child side
                            await ctx.cancel()

                        else:
                            await stream.send('yo')

                else:
                    # callee overruns caller case so we do nothing here
                    await trio.sleep_forever()

            await portal.cancel_actor()

    # 2 overrun cases and the no overrun case (which pushes right up to
    # the msg limit)
    if overrunner == 'caller':
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        assert excinfo.value.type == tractor._exceptions.StreamOverrun

    elif overrunner == 'callee':
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        assert excinfo.value.type == tractor.RemoteActorError

    else:
        trio.run(main)
