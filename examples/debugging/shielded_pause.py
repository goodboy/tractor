import trio
import tractor


async def cancellable_pause_loop(
    task_status: trio.TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED
):
    with trio.CancelScope() as cs:
        task_status.started(cs)
        for _ in range(3):
            try:
                # ON first entry, there is no level triggered
                # cancellation yet, so this cp does a parent task
                # ctx-switch so that this scope raises for the NEXT
                # checkpoint we hit.
                await trio.lowlevel.checkpoint()
                await tractor.pause()

                cs.cancel()

                # parent should have called `cs.cancel()` by now
                await trio.lowlevel.checkpoint()

            except trio.Cancelled:
                print('INSIDE SHIELDED PAUSE')
                await tractor.pause(shield=True)
        else:
            # should raise it again, bubbling up to parent
            print('BUBBLING trio.Cancelled to parent task-nursery')
            await trio.lowlevel.checkpoint()


async def pm_on_cancelled():
    async with trio.open_nursery() as tn:
        tn.cancel_scope.cancel()
        try:
            await trio.sleep_forever()
        except trio.Cancelled:
            # should also raise `Cancelled` since
            # we didn't pass `shield=True`.
            try:
                await tractor.post_mortem(hide_tb=False)
            except trio.Cancelled as taskc:

                # should enter just fine, in fact it should
                # be debugging the internals of the previous
                # sin-shield call above Bo
                await tractor.post_mortem(
                    hide_tb=False,
                    shield=True,
                )
                raise taskc

        else:
            raise RuntimeError('Dint cancel as expected!?')


async def cancelled_before_pause(
):
    '''
    Verify that using a shielded pause works despite surrounding
    cancellation called state in the calling task.

    '''
    async with trio.open_nursery() as tn:
        cs: trio.CancelScope = await tn.start(cancellable_pause_loop)
        await trio.sleep(0.1)

    assert cs.cancelled_caught

    await pm_on_cancelled()


async def main():
    async with tractor.open_nursery(
        debug_mode=True,
    ) as n:
        portal: tractor.Portal = await n.run_in_actor(
            cancelled_before_pause,
        )
        await portal.result()

        # ensure the same works in the root actor!
        await pm_on_cancelled()


if __name__ == '__main__':
    trio.run(main)
