from contextlib import (
    asynccontextmanager as acm,
)
from functools import partial

import tractor
import trio


log = tractor.log.get_logger(
    name=__name__
)

_lock: trio.Lock|None = None


@acm
async def acquire_singleton_lock(
) -> None:
    global _lock
    if _lock is None:
        log.info('Allocating LOCK')
        _lock = trio.Lock()

    log.info('TRYING TO LOCK ACQUIRE')
    async with _lock:
        log.info('ACQUIRED')
        yield _lock

    log.info('RELEASED')



async def hold_lock_forever(
    task_status=trio.TASK_STATUS_IGNORED
):
    async with (
        tractor.trionics.maybe_raise_from_masking_exc(),
        acquire_singleton_lock() as lock,
    ):
        task_status.started(lock)
        await trio.sleep_forever()


async def main(
    ignore_special_cases: bool,
    loglevel: str = 'info',
    debug_mode: bool = True,
):
    async with (
        trio.open_nursery() as tn,

        # tractor.trionics.maybe_raise_from_masking_exc()
        # ^^^ XXX NOTE, interestingly putting the unmasker
        # here does not exhibit the same behaviour ??
    ):
        if not ignore_special_cases:
            from tractor.trionics import _taskc
            _taskc._mask_cases.clear()

        _lock = await tn.start(
            hold_lock_forever,
        )
        with trio.move_on_after(0.2):
            await tn.start(
                hold_lock_forever,
            )

        tn.cancel_scope.cancel()


# XXX, manual test as script
if __name__ == '__main__':
    tractor.log.get_console_log(level='info')
    for case in [True, False]:
        log.info(
            f'\n'
            f'------ RUNNING SCRIPT TRIAL ------\n'
            f'ignore_special_cases: {case!r}\n'
        )
        trio.run(partial(
            main,
            ignore_special_cases=case,
            loglevel='info',
        ))
