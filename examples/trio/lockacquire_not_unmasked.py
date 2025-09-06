from functools import partial

import tractor
import trio


log = tractor.log.get_logger(
    name=__name__
)


async def acquire_singleton_lock(
    _lock = trio.Lock(),
) -> None:
    log.info('TRYING TO LOCK ACQUIRE')
    await _lock.acquire()
    log.info('ACQUIRED')


@tractor.context
async def acquire_actor_global_lock(
    ctx: tractor.Context,

    ignore_special_cases: bool,
):
    if not ignore_special_cases:
        from tractor.trionics import _taskc
        _taskc._mask_cases.clear()

    await acquire_singleton_lock()
    await ctx.started('locked')

    # block til cancelled
    await trio.sleep_forever()


async def main(
    ignore_special_cases: bool,
    loglevel: str = 'info',
    debug_mode: bool = True,

    _fail_after: float = 2,
):
    tractor.log.get_console_log(level=loglevel)

    with trio.fail_after(_fail_after):
        async with (
            tractor.trionics.collapse_eg(),
            tractor.open_nursery(
                debug_mode=debug_mode,
                loglevel=loglevel,
            ) as an,
            trio.open_nursery() as tn,
        ):
            ptl = await an.start_actor(
                'locker',
                enable_modules=[__name__],
            )

            async def _open_ctx(
                task_status=trio.TASK_STATUS_IGNORED,
            ):
                async with ptl.open_context(
                    acquire_actor_global_lock,
                    ignore_special_cases=ignore_special_cases,
                ) as pair:
                    task_status.started(pair)
                    await trio.sleep_forever()

            first_ctx, first = await tn.start(_open_ctx,)
            assert first == 'locked'

            with trio.move_on_after(0.5):# as cs:
                await _open_ctx()

            # await tractor.pause()
            print('cancelling first IPC ctx!')
            await first_ctx.cancel()

            await ptl.cancel_actor()
            # await tractor.pause()


# XXX, manual test as script
if __name__ == '__main__':
    tractor.log.get_console_log(level='info')
    for case in [False, True]:
        log.info(
            f'\n'
            f'------ RUNNING SCRIPT TRIAL ------\n'
            f'child_errors_midstream: {case!r}\n'
        )
        trio.run(partial(
            main,
            ignore_special_cases=case,
            loglevel='info',
        ))
