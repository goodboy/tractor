'''
``async with ():`` inlined context-stream cancellation testing.

Verify the we raise errors when streams are opened prior to sync-opening
a ``tractor.Context`` beforehand.

'''
import pytest
import trio
from trio.lowlevel import current_task
import tractor


@tractor.context
async def really_started(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    try:
        await ctx.started()
    except RuntimeError as err:
        raise


def test_started_called_more_then_once():

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'too_much_starteds',
                enable_modules=[__name__],
            )

            async with portal.open_context(really_started) as (ctx, sent):
                pass

    with pytest.raises(tractor.RemoteActorError) as excinfo:
        trio.run(main)


@tractor.context
async def never_open_stream(

    ctx:  tractor.Context,

) -> None:
    '''Bidir streaming endpoint which will stream
    back any sequence it is sent item-wise.

    '''
    await ctx.started()
    await trio.sleep_forever()


def test_no_far_end_stream_opened():
    '''
    This should exemplify the bug from:
    https://github.com/goodboy/tractor/issues/265

    '''
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'starts_no_stream',
                enable_modules=[__name__],
            )

            async with (
                portal.open_context(
                    never_open_stream,) as (ctx, sent),
                ctx.open_stream() as stream,
            ):
                assert sent is None

                # XXX: so the question is whether
                # this should error if the far end
                # has not yet called `ctx.open_stream()`?
                # If we decide to do that we need a synchronization
                # message which is sent from that call?
                await stream.send('yo')

    trio.run(main)
