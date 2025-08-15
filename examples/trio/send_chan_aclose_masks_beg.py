from contextlib import (
    contextmanager as cm,
    # TODO, any diff in async case(s)??
    # asynccontextmanager as acm,
)
from functools import partial

import tractor
import trio


log = tractor.log.get_logger(__name__)
tractor.log.get_console_log('info')

@cm
def teardown_on_exc(
    raise_from_handler: bool = False,
):
    '''
    You could also have a teardown handler which catches any exc and
    does some required teardown. In this case the problem is
    compounded UNLESS you ensure the handler's scope is OUTSIDE the
    `ux.aclose()`.. that is in the caller's enclosing scope.

    '''
    try:
        yield
    except BaseException as _berr:
        berr = _berr
        log.exception(
            f'Handling termination teardown in child due to,\n'
            f'{berr!r}\n'
        )
        if raise_from_handler:
            # XXX teardown ops XXX
            # on termination these steps say need to be run to
            # ensure wider system consistency (like the state of
            # remote connections/services).
            #
            # HOWEVER, any bug in this teardown code is also
            # masked by the `tx.aclose()`!
            # this is also true if `_tn.cancel_scope` is
            # `.cancel_called` by the parent in a graceful
            # request case..

            # simulate a bug in teardown handler.
            raise RuntimeError(
                'woopsie teardown bug!'
            )

        raise  # no teardown bug.


async def finite_stream_to_rent(
    tx: trio.abc.SendChannel,
    child_errors_mid_stream: bool,

    task_status: trio.TaskStatus[
        trio.CancelScope,
    ] = trio.TASK_STATUS_IGNORED,
):
    async with (
        # XXX without this unmasker the mid-streaming RTE is never
        # reported since it is masked by the `tx.aclose()`
        # call which in turn raises `Cancelled`!
        #
        # NOTE, this is WITHOUT doing any exception handling
        # inside the child  task!
        #
        # TODO, uncomment next LoC to see the supprsessed beg[RTE]!
        # tractor.trionics.maybe_raise_from_masking_exc(),

        tx as tx,  # .aclose() is the guilty masker chkpt!
        trio.open_nursery() as _tn,
    ):
        # pass our scope back to parent for supervision\
        # control.
        task_status.started(_tn.cancel_scope)

        with teardown_on_exc(
            raise_from_handler=not child_errors_mid_stream,
        ):
            for i in range(100):
                log.info(
                    f'Child tx {i!r}\n'
                )
                if (
                    child_errors_mid_stream
                    and
                    i == 66
                ):
                    # oh wait but WOOPS there's a bug
                    # in that teardown code!?
                    raise RuntimeError(
                        'woopsie, a mid-streaming bug!?'
                    )

                await tx.send(i)


async def main(
    # TODO! toggle this for the 2 cases!
    # 1. child errors mid-stream while parent is also requesting
    #   (graceful) cancel of that child streamer.
    #
    # 2. child contains a teardown handler which contains a
    #   bug and raises.
    #
    child_errors_mid_stream: bool,
):
    tx, rx = trio.open_memory_channel(1)

    async with (
        trio.open_nursery() as tn,
        rx as rx,
    ):

        _child_cs = await tn.start(
            partial(
                finite_stream_to_rent,
                child_errors_mid_stream=child_errors_mid_stream,
                tx=tx,
            )
        )
        async for msg in rx:
            log.info(
                f'Rent rx {msg!r}\n'
            )

            # simulate some external cancellation
            # request **JUST BEFORE** the child errors.
            if msg == 65:
                log.cancel(
                    f'Cancelling parent on,\n'
                    f'msg={msg}\n'
                    f'\n'
                    f'Simulates OOB cancel request!\n'
                )
                tn.cancel_scope.cancel()


if __name__ == '__main__':

    for case in [True, False]:
        trio.run(main, case)
