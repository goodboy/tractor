'''
Verify we can dump a `stackscope` tree on a hang.

'''
import os
import signal

import trio
import tractor

@tractor.context
async def start_n_shield_hang(
    ctx: tractor.Context,
):
    # actor: tractor.Actor = tractor.current_actor()

    # sync to parent-side task
    await ctx.started(os.getpid())

    print('Entering shield sleep..')
    with trio.CancelScope(shield=True):
        await trio.sleep_forever()  # in subactor

    # XXX NOTE ^^^ since this shields, we expect
    # the zombie reaper (aka T800) to engage on
    # SIGINT from the user and eventually hard-kill
    # this subprocess!


async def main(
    from_test: bool = False,
) -> None:

    async with (
        tractor.open_nursery(
            debug_mode=True,
            enable_stack_on_sig=True,
            # maybe_enable_greenback=False,
            loglevel='devx',
        ) as an,
    ):

        ptl: tractor.Portal  = await an.start_actor(
            'hanger',
            enable_modules=[__name__],
            debug_mode=True,
        )
        async with ptl.open_context(
            start_n_shield_hang,
        ) as (ctx, cpid):

            _, proc, _ = an._children[ptl.chan.uid]
            assert cpid == proc.pid

            print(
                'Yo my child hanging..?\n'
                'Sending SIGUSR1 to see a tree-trace!\n'
            )

            # XXX simulate the wrapping test's "user actions"
            # (i.e. if a human didn't run this manually but wants to
            # know what they should do to reproduce test behaviour)
            if from_test:
                os.kill(
                    cpid,
                    signal.SIGUSR1,
                )

                # simulate user cancelling program
                await trio.sleep(0.5)
                os.kill(
                    os.getpid(),
                    signal.SIGINT,
                )
            else:
                # actually let user send the ctl-c
                await trio.sleep_forever()  # in root


if __name__ == '__main__':
    trio.run(main)
