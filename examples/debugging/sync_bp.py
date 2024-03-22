import trio
import tractor


def sync_pause(
    use_builtin: bool = True,
    error: bool = False,
):
    if use_builtin:
        breakpoint()

    else:
        tractor.pause_from_sync()

    if error:
        raise RuntimeError('yoyo sync code error')


@tractor.context
async def start_n_sync_pause(
    ctx: tractor.Context,
):
    # sync to requesting peer
    await ctx.started()

    actor: tractor.Actor = tractor.current_actor()
    print(f'entering SYNC PAUSE in {actor.uid}')
    sync_pause()
    print(f'back from SYNC PAUSE in {actor.uid}')


async def main() -> None:

    async with tractor.open_nursery(
        debug_mode=True,
    ) as an:

        p: tractor.Portal  = await an.start_actor(
            'subactor',
            enable_modules=[__name__],
            # infect_asyncio=True,
            debug_mode=True,
            loglevel='cancel',
        )

        # TODO: 3 sub-actor usage cases:
        # -[ ] via a `.run_in_actor()` call
        # -[ ] via a `.run()`
        # -[ ] via a `.open_context()`
        #
        async with p.open_context(
            start_n_sync_pause,
        ) as (ctx, first):
            assert first is None

            await tractor.pause()
            sync_pause()

        # TODO: make this work!!
        await trio.to_thread.run_sync(
            sync_pause,
            abandon_on_cancel=False,
        )

        await ctx.cancel()

        # TODO: case where we cancel from trio-side while asyncio task
        # has debugger lock?
        await p.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
