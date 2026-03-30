import itertools

import pytest
import trio
import tractor
from tractor import open_actor_cluster
from tractor.trionics import gather_contexts
from tractor._testing import tractor_test

MESSAGE = 'tractoring at full speed'


def test_empty_mngrs_input_raises(
    tpt_proto: str,
) -> None:
    # TODO, the `open_actor_cluster()` teardown hangs
    # intermittently on UDS when `gather_contexts(mngrs=())`
    # raises `ValueError` mid-setup; likely a race in the
    # actor-nursery cleanup vs UDS socket shutdown. Needs
    # a deeper look at `._clustering`/`._supervise` teardown
    # paths with the UDS transport.
    if tpt_proto == 'uds':
        pytest.skip(
            'actor-cluster teardown hangs intermittently on UDS'
        )

    async def main():
        with trio.fail_after(3):
            async with (
                open_actor_cluster(
                    modules=[__name__],

                    # NOTE: ensure we can passthrough runtime opts
                    loglevel='cancel',
                    debug_mode=False,

                ) as portals,

                gather_contexts(mngrs=()),
            ):
                # should fail before this?
                assert portals

                # test should fail if we mk it here!
                assert 0, 'Should have raised val-err !?'

    with pytest.raises(ValueError):
        trio.run(main)


@tractor.context
async def worker(
    ctx: tractor.Context,

) -> None:

    await ctx.started()

    async with ctx.open_stream(
        allow_overruns=True,
    ) as stream:

        # TODO: this with the below assert causes a hang bug?
        # with trio.move_on_after(1):

        async for msg in stream:
            # do something with msg
            print(msg)
            assert msg == MESSAGE

        # ?TODO, does this ever cause a hang?
        # assert 0


# ?TODO, but needs a fn-scoped tpt_proto fixture..
# @pytest.mark.no_tpt('uds')
@tractor_test
async def test_streaming_to_actor_cluster(
    tpt_proto: str,
):
    '''
    Open an actor "cluster" using the (experimental) `._clustering`
    API and conduct standard inter-task-ctx streaming.

    '''
    if tpt_proto == 'uds':
        pytest.skip(
            f'Test currently fails with tpt-proto={tpt_proto!r}\n'
        )

    with trio.fail_after(6):
        async with (
            open_actor_cluster(modules=[__name__]) as portals,

            gather_contexts(
                mngrs=[p.open_context(worker) for p in portals.values()],
            ) as contexts,

            gather_contexts(
                mngrs=[ctx[0].open_stream() for ctx in contexts],
            ) as streams,

        ):
            with trio.move_on_after(1):
                for stream in itertools.cycle(streams):
                    await stream.send(MESSAGE)
