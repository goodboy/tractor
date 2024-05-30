import trio
import tractor


@tractor.context
async def name_error(
    ctx: tractor.Context,
):
    '''
    Raise a `NameError`, catch it and enter `.post_mortem()`, then
    expect the `._rpc._invoke()` crash handler to also engage.

    '''
    try:
        getattr(doggypants)  # noqa (on purpose)
    except NameError:
        await tractor.post_mortem()
        raise


async def main():
    '''
    Test 3 `PdbREPL` entries:
      - one in the child due to manual `.post_mortem()`,
      - another in the child due to runtime RPC crash handling.
      - final one here in parent from the RAE.

    '''
    # XXX NOTE: ideally the REPL arrives at this frame in the parent
    # ONE UP FROM the inner ctx block below!
    async with tractor.open_nursery(
        debug_mode=True,
        # loglevel='cancel',
    ) as an:
        p: tractor.Portal = await an.start_actor(
            'child',
            enable_modules=[__name__],
        )

        # XXX should raise `RemoteActorError[NameError]`
        # AND be the active frame when REPL enters!
        try:
            async with p.open_context(name_error) as (ctx, first):
                assert first
        except tractor.RemoteActorError as rae:
            assert rae.boxed_type is NameError

            # manually handle in root's parent task
            await tractor.post_mortem()
            raise
        else:
            raise RuntimeError('IPC ctx should have remote errored!?')


if __name__ == '__main__':
    trio.run(main)
