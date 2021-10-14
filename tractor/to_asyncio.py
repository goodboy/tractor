'''
Infection apis for ``asyncio`` loops running ``trio`` using guest mode.

'''
import asyncio
from contextlib import asynccontextmanager as acm
import inspect
from typing import (
    Any,
    Callable,
    AsyncIterator,
    Awaitable,
    Optional,
)

import trio

from .log import get_logger, get_console_log
from ._state import current_actor

log = get_logger(__name__)


__all__ = ['run_task', 'run_as_asyncio_guest']


# async def consume_asyncgen(
#     to_trio: trio.MemorySendChannel,
#     coro: AsyncIterator,
# ) -> None:
#     """Stream async generator results back to ``trio``.

#     ``from_trio`` might eventually be used here for
#     bidirectional streaming.
#     """
#     async for item in coro:
#         to_trio.send_nowait(item)


def _run_asyncio_task(
    func: Callable,
    *,
    qsize: int = 1,
    # _treat_as_stream: bool = False,
    provide_channels: bool = False,
    **kwargs,

) -> Any:
    """
    Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.

    """
    if not current_actor().is_infected_aio():
        raise RuntimeError("`infect_asyncio` mode is not enabled!?")

    # ITC (inter task comms)
    from_trio = asyncio.Queue(qsize)  # type: ignore
    to_trio, from_aio = trio.open_memory_channel(qsize)  # type: ignore

    from_aio._err = None

    args = tuple(inspect.getfullargspec(func).args)

    if getattr(func, '_tractor_steam_function', None):
        # the assumption is that the target async routine accepts the
        # send channel then it intends to yield more then one return
        # value otherwise it would just return ;P
        # _treat_as_stream = True
        assert qsize > 1

    if provide_channels:
        assert 'to_trio' in args

    # allow target func to accept/stream results manually by name
    if 'to_trio' in args:
        kwargs['to_trio'] = to_trio

    if 'from_trio' in args:
        kwargs['from_trio'] = from_trio

    coro = func(**kwargs)

    cancel_scope = trio.CancelScope()
    aio_task_complete = trio.Event()
    aio_err: Optional[BaseException] = None

    async def wait_on_coro_final_result(
        to_trio: trio.MemorySendChannel,
        coro: Awaitable,
        aio_task_complete: trio.Event,

    ) -> None:
        """
        Await ``coro`` and relay result back to ``trio``.

        """
        nonlocal aio_err
        orig = result = id(coro)
        try:
            result = await coro
        except BaseException as err:
            aio_err = err
            from_aio._err = aio_err
            raise
        finally:
            aio_task_complete.set()
            if result != orig and aio_err is None:
                to_trio.send_nowait(result)

    # start the asyncio task we submitted from trio
    if inspect.isawaitable(coro):
        task = asyncio.create_task(
            wait_on_coro_final_result(to_trio, coro, aio_task_complete)
        )

    # elif inspect.isasyncgen(coro):
    #     task = asyncio.create_task(consume_asyncgen(to_trio, coro))

    else:
        raise TypeError(f"No support for invoking {coro}")

    def cancel_trio(task) -> None:
        '''
        Cancel the calling ``trio`` task on error.

        '''
        nonlocal aio_err
        try:
            aio_err = task.exception()
        except asyncio.CancelledError as cerr:
            aio_err = cerr

        if aio_err:
            log.exception(f"asyncio task errorred:\n{aio_err}")
            from_aio._err = aio_err
            cancel_scope.cancel()
            from_aio.close()

    task.add_done_callback(cancel_trio)

    return task, from_aio, to_trio, cancel_scope, aio_task_complete


async def run_task(
    func: Callable,
    *,

    qsize: int = 2**10,
    # _treat_as_stream: bool = False,
    **kwargs,

) -> Any:
    """Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.

    """
    # simple async func
    try:
        task, from_aio, to_trio, cs, _ = _run_asyncio_task(
            func,
            qsize=1,
            **kwargs,
        )

        # return single value
        with cs:
            # naively expect the mem chan api to do the job
            # of handling cross-framework cancellations / errors
            return await from_aio.receive()

        if cs.cancelled_caught:
            # always raise from any captured asyncio error
            if from_aio._err:
                raise from_aio._err

    # Do we need this?
    except BaseException as err:

        aio_err = from_aio._err

        if aio_err is not None:
            # always raise from any captured asyncio error
            raise err from aio_err
        else:
            raise

    # except trio.Cancelled:
        # raise
    finally:
        if not task.done():
            task.cancel()


# TODO: explicit api for the streaming case where
# we pull from the mem chan in an async generator?
# This ends up looking more like our ``Portal.open_stream_from()``
# NB: code below is untested.

# async def _start_and_sync_aio_task(
#     from_trio,
#     to_trio,
#     from_aio,


@acm
async def open_channel_from(

    target: Callable[[Any, ...], Any],
    **kwargs,

) -> AsyncIterator[Any]:

    try:
        task, from_aio, to_trio, cs, aio_task_complete = _run_asyncio_task(
            target,
            qsize=2**8,
            provide_channels=True,
            **kwargs,
        )

        with cs:
            # sync to "started()" call.
            first = await from_aio.receive()
            # stream values upward
            async with from_aio:
                yield first, from_aio
                # await aio_task_complete.wait()

    except BaseException as err:

        aio_err = from_aio._err

        if aio_err is not None:
            # always raise from any captured asyncio error
            raise err from aio_err
        else:
            raise

    finally:
        if cs.cancelled_caught:
            # always raise from any captured asyncio error
            if from_aio._err:
                raise from_aio._err

        if not task.done():
            task.cancel()


def run_as_asyncio_guest(
    trio_main: Callable,
) -> None:
    """Entry for an "infected ``asyncio`` actor".

    Uh, oh. :o

    It looks like your event loop has caught a case of the ``trio``s.

    :()

    Don't worry, we've heard you'll barely notice. You might hallucinate
    a few more propagating errors and feel like your digestion has
    slowed but if anything get's too bad your parents will know about
    it.

    :)

    """
    # Disable sigint handling in children?
    # import signal
    # signal.signal(signal.SIGINT, signal.SIG_IGN)

    get_console_log('runtime')

    async def aio_main(trio_main):

        loop = asyncio.get_running_loop()
        trio_done_fut = asyncio.Future()

        def trio_done_callback(main_outcome):

            print(f"trio_main finished: {main_outcome!r}")
            trio_done_fut.set_result(main_outcome)

        # start the infection: run trio on the asyncio loop in "guest mode"
        log.info(f"Infecting asyncio process with {trio_main}")

        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )
        (await trio_done_fut).unwrap()

    # might as well if it's installed.
    try:
        import uvloop
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)
    except ImportError:
        pass

    asyncio.run(aio_main(trio_main))
