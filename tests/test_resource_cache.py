'''
Async context manager cache api testing: ``trionics.maybe_open_context():``

'''
from contextlib import asynccontextmanager as acm
import platform
from typing import Awaitable

import pytest
import trio
import tractor


_resource: int = 0


@acm
async def maybe_increment_counter(task_name: str):
    global _resource

    _resource += 1
    await trio.lowlevel.checkpoint()
    yield _resource
    await trio.lowlevel.checkpoint()
    _resource -= 1


@pytest.mark.parametrize(
    'key_on',
    ['key_value', 'kwargs'],
    ids="key_on={}".format,
)
def test_resource_only_entered_once(key_on):
    global _resource
    _resource = 0

    key = None
    if key_on == 'key_value':
        key = 'some_common_key'

    async def main():
        cache_active: bool = False

        async def enter_cached_mngr(name: str):
            nonlocal cache_active

            if key_on == 'kwargs':
                # make a common kwargs input to key on it
                kwargs = {'task_name': 'same_task_name'}
                assert key is None
            else:
                # different task names per task will be used
                kwargs = {'task_name': name}

            async with tractor.trionics.maybe_open_context(
                maybe_increment_counter,
                kwargs=kwargs,
                key=key,

            ) as (cache_hit, resource):
                if cache_hit:
                    try:
                        cache_active = True
                        assert resource == 1
                        await trio.sleep_forever()
                    finally:
                        cache_active = False
                else:
                    assert resource == 1
                    await trio.sleep_forever()

        with trio.move_on_after(0.5):
            async with (
                tractor.open_root_actor(),
                trio.open_nursery() as tn,
            ):
                for i in range(10):
                    tn.start_soon(
                        enter_cached_mngr,
                        f'task_{i}',
                    )
                    await trio.sleep(0.001)

    trio.run(main)


@tractor.context
async def streamer(
    ctx: tractor.Context,
    seq: list[int] = list(range(1000)),
) -> None:

    await ctx.started()
    async with ctx.open_stream() as stream:
        for val in seq:
            await stream.send(val)
            await trio.sleep(0.001)

    print('producer finished')


@acm
async def open_stream() -> Awaitable[
    tuple[
        tractor.ActorNursery,
        tractor.MsgStream,
    ]
]:
    try:
        async with tractor.open_nursery() as an:
            portal = await an.start_actor(
                'streamer',
                enable_modules=[__name__],
            )
            try:
                async with (
                    portal.open_context(streamer) as (ctx, first),
                    ctx.open_stream() as stream,
                ):
                    print('Entered open_stream() caller')
                    yield an, stream
                    print('Exited open_stream() caller')
                    # await tractor.pause(shield=True)

            finally:
                print('Cancelling streamer')
                # await tractor.pause(shield=True)
                with trio.CancelScope(shield=True):
                    await portal.cancel_actor()
                print('Cancelled streamer')

    except Exception as err:
        print(
            f'`open_stream()` errored?\n'
            f'{err!r}\n'
        )
        await tractor.pause(shield=True)
        raise err


@acm
async def maybe_open_stream(taskname: str):
    async with tractor.trionics.maybe_open_context(
        # NOTE: all secondary tasks should cache hit on the same key
        acm_func=open_stream,
    ) as (
        cache_hit,
        (an, stream)
    ):
        if cache_hit:
            print(f'{taskname} loaded from cache')

            # add a new broadcast subscription for the quote stream
            # if this feed is already allocated by the first
            # task that entereed
            async with stream.subscribe() as bstream:
                yield an, bstream
                print(
                    f'cached task exited\n'
                    f')>\n'
                    f' |_{taskname}\n'
                )
        else:
            # yield the actual stream
            try:
                yield an, stream
            finally:
                print(
                    f'NON-cached task exited\n'
                    f')>\n'
                    f' |_{taskname}\n'
                )

        bstream = stream._broadcaster
        if (
            not bstream.state.subs
            and
            not cache_hit
        ):
            await tractor.pause(shield=True)
            # await an.cancel()



def test_open_local_sub_to_stream(
    debug_mode: bool,
):
    '''
    Verify a single inter-actor stream can can be fanned-out shared to
    N local tasks using `trionics.maybe_open_context()`.

    '''
    timeout: float = 3.6
    if platform.system() == "Windows":
        timeout: float = 10

    if debug_mode:
        timeout = 999
        print(f'IN debug_mode, setting large timeout={timeout!r}..')

    async def main():

        full = list(range(1000))
        an: tractor.ActorNursery|None = None
        num_tasks: int = 10

        async def get_sub_and_pull(taskname: str):

            nonlocal an

            stream: tractor.MsgStream
            async with (
                maybe_open_stream(taskname) as (
                    an,
                    stream,
                ),
            ):
                if '0' in taskname:
                    assert isinstance(stream, tractor.MsgStream)
                else:
                    assert isinstance(
                        stream,
                        tractor.trionics.BroadcastReceiver
                    )

                first = await stream.receive()
                print(f'{taskname} started with value {first}')
                seq: list[int] = []
                async for msg in stream:
                    seq.append(msg)

                assert set(seq).issubset(set(full))

            # end of @acm block
            print(f'{taskname} finished')

        root: tractor.Actor
        with trio.fail_after(timeout) as cs:
            # TODO: turns out this isn't multi-task entrant XD
            # We probably need an indepotent entry semantic?
            async with tractor.open_root_actor(
                debug_mode=debug_mode,
                # maybe_enable_greenback=True,
                #
                # ^TODO? doesn't seem to mk breakpoint() usage work
                # bc each bg task needs to open a portal??
                # - [ ] we should consider making this part of
                #      our taskman defaults?
                #   |_see https://github.com/goodboy/tractor/pull/363
                #
            ) as root:
                assert root.is_registrar

                async with (
                    trio.open_nursery() as tn,
                ):
                    for i in range(num_tasks):
                        tn.start_soon(
                            get_sub_and_pull,
                            f'task_{i}',
                        )
                        await trio.sleep(0.001)

                print('all consumer tasks finished.')

                # ensure actor-nursery is shutdown or we might
                # hang here..?
                # if root.ipc_server.has_peers():
                #     await tractor.pause()
                #     await an.cancel()

            # end of runtime scope
            print('root actor terminated.')

        if cs.cancelled_caught:
            pytest.fail(
                'Should NOT time out in `open_root_actor()` ?'
            )

        print('exiting main.')

    trio.run(main)
