# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Infection apis for ``asyncio`` loops running ``trio`` using guest mode.

'''
from __future__ import annotations
import asyncio
from asyncio.exceptions import CancelledError
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass
import inspect
import traceback
from typing import (
    Any,
    Callable,
    AsyncIterator,
    Awaitable,
)

import tractor
from tractor._exceptions import (
    AsyncioCancelled,
    is_multi_cancelled,
)
from tractor._state import (
    debug_mode,
    _runtime_vars,
)
from tractor.devx import _debug
from tractor.log import (
    get_logger,
    StackLevelAdapter,
)
from tractor.trionics._broadcast import (
    broadcast_receiver,
    BroadcastReceiver,
)
import trio
from outcome import (
    Error,
    Outcome,
)

log: StackLevelAdapter = get_logger(__name__)


__all__ = [
    'run_task',
    'run_as_asyncio_guest',
]


@dataclass
class LinkedTaskChannel(trio.abc.Channel):
    '''
    A "linked task channel" which allows for two-way synchronized msg
    passing between a ``trio``-in-guest-mode task and an ``asyncio``
    task scheduled in the host loop.

    '''
    _to_aio: asyncio.Queue
    _from_aio: trio.MemoryReceiveChannel
    _to_trio: trio.MemorySendChannel
    _trio_cs: trio.CancelScope
    _aio_task_complete: trio.Event

    _trio_err: BaseException|None = None
    _trio_exited: bool = False

    # set after ``asyncio.create_task()``
    _aio_task: asyncio.Task|None = None
    _aio_err: BaseException|None = None
    _broadcaster: BroadcastReceiver|None = None

    async def aclose(self) -> None:
        await self._from_aio.aclose()

    async def receive(self) -> Any:
        '''
        Receive a value from the paired `asyncio.Task` with
        exception/cancel handling to teardown both sides on any
        unexpected error.

        '''
        try:
            # TODO: do we need this to guarantee asyncio code get's
            # cancelled in the case where the trio side somehow creates
            # a state where the asyncio cycle-task isn't getting the
            # cancel request sent by (in theory) the last checkpoint
            # cycle on the trio side?
            # await trio.lowlevel.checkpoint()
            return await self._from_aio.receive()
        except BaseException as err:
            async with translate_aio_errors(
                self,

                # XXX: obviously this will deadlock if an on-going stream is
                # being procesed.
                # wait_on_aio_task=False,
            ):
                raise err

    async def wait_asyncio_complete(self) -> None:
        await self._aio_task_complete.wait()

    def cancel_asyncio_task(
        self,
        msg: str = '',
    ) -> None:
        self._aio_task.cancel(
            msg=msg,
        )

    async def send(self, item: Any) -> None:
        '''
        Send a value through to the asyncio task presuming
        it defines a ``from_trio`` argument, if it does not
        this method will raise an error.

        '''
        self._to_aio.put_nowait(item)

    def closed(self) -> bool:
        return self._from_aio._closed  # type: ignore

    # TODO: shoud we consider some kind of "decorator" system
    # that checks for structural-typing compatibliity and then
    # automatically adds this ctx-mngr-as-method machinery?
    @acm
    async def subscribe(
        self,

    ) -> AsyncIterator[BroadcastReceiver]:
        '''
        Allocate and return a ``BroadcastReceiver`` which delegates
        to this inter-task channel.

        This allows multiple local tasks to receive each their own copy
        of this message stream.

        See ``tractor._streaming.MsgStream.subscribe()`` for further
        similar details.
        '''
        if self._broadcaster is None:

            bcast = self._broadcaster = broadcast_receiver(
                self,
                # use memory channel size by default
                self._from_aio._state.max_buffer_size,  # type: ignore
                receive_afunc=self.receive,
            )

            self.receive = bcast.receive  # type: ignore

        async with self._broadcaster.subscribe() as bstream:
            assert bstream.key != self._broadcaster.key
            assert bstream._recv == self._broadcaster._recv
            yield bstream


def _run_asyncio_task(
    func: Callable,
    *,
    qsize: int = 1,
    provide_channels: bool = False,
    hide_tb: bool = False,
    **kwargs,

) -> LinkedTaskChannel:
    '''
    Run an `asyncio`-compat async function or generator in a task,
    return or stream the result back to the caller
    `trio.lowleve.Task`.

    '''
    __tracebackhide__: bool = hide_tb
    if not tractor.current_actor().is_infected_aio():
        raise RuntimeError(
            "`infect_asyncio` mode is not enabled!?"
        )

    # ITC (inter task comms), these channel/queue names are mostly from
    # ``asyncio``'s perspective.
    aio_q = from_trio = asyncio.Queue(qsize)  # type: ignore
    to_trio, from_aio = trio.open_memory_channel(qsize)  # type: ignore

    args = tuple(inspect.getfullargspec(func).args)
    if getattr(func, '_tractor_steam_function', None):
        # the assumption is that the target async routine accepts the
        # send channel then it intends to yield more then one return
        # value otherwise it would just return ;P
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
    aio_err: BaseException|None = None

    chan = LinkedTaskChannel(
        _to_aio=aio_q,  # asyncio.Queue
        _from_aio=from_aio,  # recv chan
        _to_trio=to_trio,  # send chan
        _trio_cs=cancel_scope,
        _aio_task_complete=aio_task_complete,
    )

    async def wait_on_coro_final_result(
        to_trio: trio.MemorySendChannel,
        coro: Awaitable,
        aio_task_complete: trio.Event,

    ) -> None:
        '''
        Await `coro` and relay result back to `trio`.

        This can only be run as an `asyncio.Task`!

        '''
        nonlocal aio_err
        nonlocal chan

        orig = result = id(coro)
        try:
            result = await coro
        except BaseException as aio_err:
            chan._aio_err = aio_err
            if isinstance(aio_err, CancelledError):
                log.runtime(
                    '`asyncio` task was cancelled..\n'
                )
            else:
                log.exception(
                    '`asyncio` task errored\n'
                )
            raise

        else:
            if (
                result != orig
                and
                aio_err is None
                and

                # in the `open_channel_from()` case we don't
                # relay through the "return value".
                not provide_channels
            ):
                to_trio.send_nowait(result)

        finally:
            # if the task was spawned using `open_channel_from()`
            # then we close the channels on exit.
            if provide_channels:
                # only close the sender side which will relay
                # a ``trio.EndOfChannel`` to the trio (consumer) side.
                to_trio.close()

            # import pdbp; pdbp.set_trace()
            aio_task_complete.set()
            # await asyncio.sleep(0.1)
            log.info(
                f'`asyncio` task terminated\n'
                f'x)>\n'
                f'  |_{task}\n'
            )

    # start the asyncio task we submitted from trio
    if not inspect.isawaitable(coro):
        raise TypeError(
            f'Pass the async-fn NOT a coroutine\n'
            f'{coro!r}'
        )

    task: asyncio.Task = asyncio.create_task(
        wait_on_coro_final_result(
            to_trio,
            coro,
            aio_task_complete
        )
    )
    chan._aio_task: asyncio.Task = task

    # XXX TODO XXX get this actually workin.. XD
    # -[ ] we need logic to setup `greenback` for `asyncio`-side task
    #     REPLing.. which should normally be nearly the same as for
    #     `trio`?
    # -[ ] add to a new `.devx._greenback.maybe_init_for_asyncio()`?
    if (
        debug_mode()
        and
        (greenback := _debug.maybe_import_greenback(
            force_reload=True,
            raise_not_found=False,
        ))
    ):
        log.info(
            f'Bestowing `greenback` portal for `asyncio`-task\n'
            f'{task}\n'
        )
        greenback.bestow_portal(task)

    def cancel_trio(task: asyncio.Task) -> None:
        '''
        Cancel the calling `trio` task on error.

        '''
        nonlocal chan
        aio_err: BaseException|None = chan._aio_err
        task_err: BaseException|None = None

        # only to avoid `asyncio` complaining about uncaptured
        # task exceptions
        try:
            res: Any = task.result()
            log.info(
                '`trio` received final result from {task}\n'
                f'|_{res}\n'
            )
        except BaseException as _aio_err:
            task_err: BaseException = _aio_err

            # read again AFTER the `asyncio` side errors in case
            # it was cancelled due to an error from `trio` (or
            # some other out of band exc).
            aio_err: BaseException|None = chan._aio_err

            # always true right?
            assert (
                type(_aio_err) is type(aio_err)
            ), (
                f'`asyncio`-side task errors mismatch?!?\n\n'
                f'caught: {_aio_err}\n'
                f'chan._aio_err: {aio_err}\n'
            )

            msg: str = (
                '`trio`-side reports that the `asyncio`-side '
                '{etype_str}\n'
                # ^NOTE filled in below
            )
            if isinstance(_aio_err, CancelledError):
                msg += (
                    f'c)>\n'
                    f' |_{task}\n'
                )
                log.cancel(
                    msg.format(etype_str='cancelled')
                )
            else:
                msg += (
                    f'x)>\n'
                    f' |_{task}\n'
                )
                log.exception(
                    msg.format(etype_str='errored')
                )


        if aio_err is not None:
            # import pdbp; pdbp.set_trace()
            # XXX: uhh is this true?
            # assert task_err, f'Asyncio task {task.get_name()} discrepancy!?'

            # NOTE: currently mem chan closure may act as a form
            # of error relay (at least in the `asyncio.CancelledError`
            # case) since we have no way to directly trigger a `trio`
            # task error without creating a nursery to throw one.
            # We might want to change this in the future though.
            from_aio.close()

            if task_err is None:
                assert aio_err
                # wait, wut?
                # aio_err.with_traceback(aio_err.__traceback__)

            # TODO: show when cancellation originated
            # from each side more pedantically in log-msg?
            # elif (
            #     type(aio_err) is CancelledError
            #     and  # trio was the cause?
            #     cancel_scope.cancel_called
            # ):
            #     log.cancel(
            #         'infected task was cancelled by `trio`-side'
            #     )
            #     raise aio_err from task_err

            # XXX: if not already, alway cancel the scope on a task
            # error in case the trio task is blocking on
            # a checkpoint.
            if (
                not cancel_scope.cancelled_caught
                or
                not cancel_scope.cancel_called
            ):
                # import pdbp; pdbp.set_trace()
                cancel_scope.cancel()

            if task_err:
                # XXX raise any `asyncio` side error IFF it doesn't
                # match the one we just caught from the task above!
                # (that would indicate something weird/very-wrong
                # going on?)
                if aio_err is not task_err:
                    # import pdbp; pdbp.set_trace()
                    raise aio_err from task_err

    task.add_done_callback(cancel_trio)
    return chan


class TrioTaskExited(AsyncioCancelled):
    '''
    The `trio`-side task exited without explicitly cancelling the
    `asyncio.Task` peer.

    This is very similar to how `trio.ClosedResource` acts as
    a "clean shutdown" signal to the consumer side of a mem-chan,

    https://trio.readthedocs.io/en/stable/reference-core.html#clean-shutdown-with-channels

    '''


@acm
async def translate_aio_errors(
    chan: LinkedTaskChannel,
    wait_on_aio_task: bool = False,
    cancel_aio_task_on_trio_exit: bool = True,

) -> AsyncIterator[None]:
    '''
    An error handling to cross-loop propagation context around
    `asyncio.Task` spawns via one of this module's APIs:

    - `open_channel_from()`
    - `run_task()`

    appropriately translates errors and cancels into ``trio`` land.

    '''
    trio_task = trio.lowlevel.current_task()

    aio_err: BaseException|None = None

    aio_task: asyncio.Task = chan._aio_task
    assert aio_task
    trio_err: BaseException|None = None
    try:
        yield  # back to one of the cross-loop apis
    except trio.Cancelled as taskc:
        trio_err = taskc

        # should NEVER be the case that `trio` is cancel-handling
        # BEFORE the other side's task-ref was set!?
        assert chan._aio_task

        # import pdbp; pdbp.set_trace()  # lolevel-debug

        # relay cancel through to called ``asyncio`` task
        chan._aio_err = AsyncioCancelled(
            f'trio`-side cancelled the `asyncio`-side,\n'
            f'c)>\n'
            f'  |_{trio_task}\n\n'


            f'{trio_err!r}\n'
        )

        # XXX NOTE XXX seems like we can get all sorts of unreliable
        # behaviour from `asyncio` under various cancellation
        # conditions (like SIGINT/kbi) when this is used..
        # SO FOR NOW, try to avoid it at most costs!
        #
        # aio_task.cancel(
        #     msg=f'the `trio` parent task was cancelled: {trio_task.name}'
        # )
        # raise

    # NOTE ALSO SEE the matching note in the `cancel_trio()` asyncio
    # task-done-callback.
    except (
        trio.ClosedResourceError,
        # trio.BrokenResourceError,
    ) as cre:
        trio_err = cre
        aio_err = chan._aio_err
        # import pdbp; pdbp.set_trace()

        # XXX if an underlying `asyncio.CancelledError` triggered
        # this channel close, raise our (non-`BaseException`) wrapper
        # exception (`AsyncioCancelled`) from that source error.
        if (
            # aio-side is cancelled?
            aio_task.cancelled()  # not set until it terminates??
            and
            type(aio_err) is CancelledError

            # TODO, if we want suppression of the
            # silent-exit-by-`trio` case?
            # -[ ] the parent task can also just catch it though?
            # -[ ] OR, offer a `signal_aio_side_on_exit=True` ??
            #
            # or
            # aio_err is None
            # and
            # chan._trio_exited

        ):
            raise AsyncioCancelled(
                f'asyncio`-side cancelled the `trio`-side,\n'
                f'c(>\n'
                f'  |_{aio_task}\n\n'

                f'{trio_err!r}\n'
            ) from aio_err

        # maybe the chan-closure is due to something else?
        else:
            raise

    except BaseException as _trio_err:
        trio_err = _trio_err
        log.exception(
            '`trio`-side task errored?'
        )

        entered: bool = await _debug._maybe_enter_pm(
            trio_err,
            api_frame=inspect.currentframe(),
        )
        if (
            not entered
            and
            not is_multi_cancelled(trio_err)
        ):
            log.exception('actor crashed\n')

        aio_taskc = AsyncioCancelled(
            f'`trio`-side task errored!\n'
            f'{trio_err}'
        ) #from trio_err

        try:
            aio_task.set_exception(aio_taskc)
        except (
            asyncio.InvalidStateError,
            RuntimeError,
            # ^XXX, uhh bc apparently we can't use `.set_exception()`
            # any more XD .. ??
        ):
            wait_on_aio_task = False

        # import pdbp; pdbp.set_trace()
        # raise aio_taskc from trio_err

    finally:
        # record wtv `trio`-side error transpired
        chan._trio_err = trio_err
        ya_trio_exited: bool = chan._trio_exited

        # NOTE! by default always cancel the `asyncio` task if
        # we've made it this far and it's not done.
        # TODO, how to detect if there's an out-of-band error that
        # caused the exit?
        if (
            cancel_aio_task_on_trio_exit
            and
            not aio_task.done()
            and
            aio_err

            # or the trio side has exited it's surrounding cancel scope
            # indicating the lifetime of the ``asyncio``-side task
            # should also be terminated.
            or (
                ya_trio_exited
                and
                not chan._trio_err   # XXX CRITICAL, `asyncio.Task.cancel()` is cucked man..
            )
        ):
            report: str = (
                'trio-side exited silently!'
            )
            assert not aio_err, 'WTF how did asyncio do this?!'

            # if the `trio.Task` already exited the `open_channel_from()`
            # block we ensure the asyncio-side gets signalled via an
            # explicit exception and its `Queue` is shutdown.
            if ya_trio_exited:
                chan._to_aio.shutdown()

                # pump the other side's task? needed?
                await trio.lowlevel.checkpoint()

                if (
                    not chan._trio_err
                    and
                    (fut := aio_task._fut_waiter)
                ):
                    fut.set_exception(
                        TrioTaskExited(
                            f'The peer `asyncio` task is still blocking/running?\n'
                            f'>>\n'
                            f'|_{aio_task!r}\n'
                        )
                    )
                else:
                    # from tractor._state import is_root_process
                    # if is_root_process():
                    #     breakpoint()
                    #     import pdbp; pdbp.set_trace()

                    aio_taskc_warn: str = (
                        f'\n'
                        f'MANUALLY Cancelling `asyncio`-task: {aio_task.get_name()}!\n\n'
                        f'**THIS CAN SILENTLY SUPPRESS ERRORS FYI\n\n'
                    )
                    report += aio_taskc_warn
                    # TODO XXX, figure out the case where calling this makes the
                    # `test_infected_asyncio.py::test_trio_closes_early_and_channel_exits`
                    # hang and then don't call it in that case!
                    #
                    aio_task.cancel(msg=aio_taskc_warn)

            log.warning(report)

        # Required to sync with the far end `asyncio`-task to ensure
        # any error is captured (via monkeypatching the
        # `channel._aio_err`) before calling ``maybe_raise_aio_err()``
        # below!
        #
        # XXX NOTE XXX the `task.set_exception(aio_taskc)` call above
        # MUST NOT EXCEPT or this WILL HANG!!
        #
        # so if you get a hang maybe step through and figure out why
        # it erroed out up there!
        #
        if wait_on_aio_task:
            # await chan.wait_asyncio_complete()
            await chan._aio_task_complete.wait()
            log.info(
                'asyncio-task is done and unblocked trio-side!\n'
            )

        # TODO?
        # -[ ] make this a channel method, OR
        # -[ ] just put back inline below?
        #
        def maybe_raise_aio_side_err(
            trio_err: Exception,
        ) -> None:
            '''
            Raise any `trio`-side-caused cancellation or legit task
            error normally propagated from the caller of either,
              - `open_channel_from()`
              - `run_task()`

            '''
            aio_err: BaseException|None = chan._aio_err

            # Check if the asyncio-side is the cause of the trio-side
            # error.
            if (
                aio_err is not None
                and
                type(aio_err) is not AsyncioCancelled

                # not isinstance(aio_err, CancelledError)
                # type(aio_err) is not CancelledError
            ):
                # always raise from any captured asyncio error
                if trio_err:
                    raise trio_err from aio_err

                raise aio_err

            if trio_err:
                raise trio_err

        # NOTE: if any ``asyncio`` error was caught, raise it here inline
        # here in the ``trio`` task
        # if trio_err:
        maybe_raise_aio_side_err(
            trio_err=trio_err
        )


async def run_task(
    func: Callable,
    *,

    qsize: int = 2**10,
    **kwargs,

) -> Any:
    '''
    Run an `asyncio`-compat async function or generator in a task,
    return or stream the result back to `trio`.

    '''
    # simple async func
    chan: LinkedTaskChannel = _run_asyncio_task(
        func,
        qsize=1,
        **kwargs,
    )
    with chan._from_aio:
        async with translate_aio_errors(
            chan,
            wait_on_aio_task=True,
        ):
            # return single value that is the output from the
            # ``asyncio`` function-as-task. Expect the mem chan api to
            # do the job of handling cross-framework cancellations
            # / errors via closure and translation in the
            # ``translate_aio_errors()`` in the above ctx mngr.
            return await chan.receive()


@acm
async def open_channel_from(

    target: Callable[..., Any],
    **kwargs,

) -> AsyncIterator[Any]:
    '''
    Open an inter-loop linked task channel for streaming between a target
    spawned ``asyncio`` task and ``trio``.

    '''
    chan: LinkedTaskChannel = _run_asyncio_task(
        target,
        qsize=2**8,
        provide_channels=True,
        **kwargs,
    )
    # TODO, tuple form here?
    async with chan._from_aio:
        async with translate_aio_errors(
            chan,
            wait_on_aio_task=True,
        ):
            # sync to a "started()"-like first delivered value from the
            # ``asyncio`` task.
            try:
                with chan._trio_cs:
                    first = await chan.receive()

                    # deliver stream handle upward
                    yield first, chan
            finally:
                chan._trio_exited = True
                chan._to_trio.close()


class AsyncioRuntimeTranslationError(RuntimeError):
    '''
    We failed to correctly relay runtime semantics and/or maintain SC
    supervision rules cross-event-loop.

    '''


def run_trio_task_in_future(
    async_fn,
    *args,
) -> asyncio.Future:
    '''
    Run an async-func as a `trio` task from an `asyncio.Task` wrapped
    in a `asyncio.Future` which is returned to the caller.

    Another astounding feat by the great @oremanj !!

    Bo

    '''
    result_future = asyncio.Future()
    cancel_scope = trio.CancelScope()
    finished: bool = False

    # monkey-patch the future's `.cancel()` meth to
    # allow cancellation relay to `trio`-task.
    cancel_message: str|None = None
    orig_cancel = result_future.cancel

    def wrapped_cancel(
        msg: str|None = None,
    ):
        nonlocal cancel_message
        if finished:
            # We're being called back after the task completed
            if msg is not None:
                return orig_cancel(msg)
            elif cancel_message is not None:
                return orig_cancel(cancel_message)
            else:
                return orig_cancel()

        if result_future.done():
            return False

        # Forward cancellation to the Trio task, don't mark
        # future as cancelled until it completes
        cancel_message = msg
        cancel_scope.cancel()
        return True

    result_future.cancel = wrapped_cancel

    async def trio_task() -> None:
        nonlocal finished
        try:
            with cancel_scope:
                try:
                    # TODO: type this with new tech in 3.13
                    result: Any = await async_fn(*args)
                finally:
                    finished = True

            # Propagate result or cancellation to the Future
            if cancel_scope.cancelled_caught:
                result_future.cancel()

            elif not result_future.cancelled():
                result_future.set_result(result)

        except BaseException as exc:
            # the result future gets all the non-Cancelled
            # exceptions. Any Cancelled need to keep propagating
            # out of this stack frame in order to reach the cancel
            # scope for which they're intended.
            cancelled: BaseException|None
            rest: BaseException|None
            if isinstance(exc, BaseExceptionGroup):
                cancelled, rest = exc.split(trio.Cancelled)

            elif isinstance(exc, trio.Cancelled):
                cancelled, rest = exc, None

            else:
                cancelled, rest = None, exc

            if not result_future.cancelled():
                if rest:
                    result_future.set_exception(rest)
                else:
                    result_future.cancel()

            if cancelled:
                raise cancelled

    trio.lowlevel.spawn_system_task(
        trio_task,
        name=async_fn,
    )
    return result_future


def run_as_asyncio_guest(
    trio_main: Callable,
    # ^-NOTE-^ when spawned with `infected_aio=True` this func is
    # normally `Actor._async_main()` as is passed by some boostrap
    # entrypoint like `._entry._trio_main()`.

    _sigint_loop_pump_delay: float = 0,

) -> None:
# ^-TODO-^ technically whatever `trio_main` returns.. we should
# try to use func-typevar-params at leaast by 3.13!
# -[ ] https://typing.readthedocs.io/en/latest/spec/callables.html#callback-protocols
# -[ ] https://peps.python.org/pep-0646/#using-type-variable-tuples-in-functions
# -[ ] https://typing.readthedocs.io/en/latest/spec/callables.html#unpack-for-keyword-arguments
# -[ ] https://peps.python.org/pep-0718/
    '''
    Entry for an "infected ``asyncio`` actor".

    Entrypoint for a Python process which starts the ``asyncio`` event
    loop and runs ``trio`` in guest mode resulting in a system where
    ``trio`` tasks can control ``asyncio`` tasks whilst maintaining
    SC semantics.

    '''
    # Uh, oh.
    #
    # :o
    #
    # looks like your stdlib event loop has caught a case of "the trios" !
    #
    # :O
    #
    # Don't worry, we've heard you'll barely notice.
    #
    # :)
    #
    # You might hallucinate a few more propagating errors and feel
    # like your digestion has slowed, but if anything get's too bad
    # your parents will know about it.
    #
    # B)
    #
    async def aio_main(trio_main):
        '''
        Main `asyncio.Task` which calls
        `trio.lowlevel.start_guest_run()` to "infect" the `asyncio`
        event-loop by embedding the `trio` scheduler allowing us to
        boot the `tractor` runtime and connect back to our parent.

        '''
        loop = asyncio.get_running_loop()
        trio_done_fute = asyncio.Future()
        startup_msg: str = (
            'Starting `asyncio` guest-loop-run\n'
            '-> got running loop\n'
            '-> built a `trio`-done future\n'
        )

        # TODO: is this evern run or needed?
        # -[ ] pretty sure it never gets run for root-infected-aio
        #     since this main task is always the parent of any
        #     eventual `open_root_actor()` call?
        if debug_mode():
            log.error(
                'Attempting to enter non-required `greenback` init '
                'from `asyncio` task ???'
            )
            # XXX make it obvi we know this isn't supported yet!
            assert 0
            # await _debug.maybe_init_greenback(
            #     force_reload=True,
            # )

        def trio_done_callback(main_outcome):
            log.runtime(
                f'`trio` guest-run finishing with outcome\n'
                f'>) {main_outcome}\n'
                f'|_{trio_done_fute}\n'
            )

            if isinstance(main_outcome, Error):
                # import pdbp; pdbp.set_trace()
                error: BaseException = main_outcome.error

                # show an dedicated `asyncio`-side tb from the error
                tb_str: str = ''.join(traceback.format_exception(error))
                log.exception(
                    'Guest-run errored!?\n\n'
                    f'{main_outcome}\n'
                    f'{error}\n\n'
                    f'{tb_str}\n'
                )
                trio_done_fute.set_exception(error)

                # raise inline
                main_outcome.unwrap()

            else:
                trio_done_fute.set_result(main_outcome)

            log.info(
                f'`trio` guest-run finished with,\n'
                f')>\n'
                f'|_{trio_done_fute}\n'
            )

        startup_msg += (
            f'-> created {trio_done_callback!r}\n'
            f'-> scheduling `trio_main`: {trio_main!r}\n'
        )

        # start the infection: run trio on the asyncio loop in "guest mode"
        log.runtime(
            f'{startup_msg}\n\n'
            +
            'Infecting `asyncio`-process with a `trio` guest-run!\n'
        )

        # TODO, somehow bootstrap this!
        _runtime_vars['_is_infected_aio'] = True

        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )
        fute_err: BaseException|None = None
        try:
            out: Outcome = await asyncio.shield(trio_done_fute)
            # ^TODO still don't really understand why the `.shield()`
            # is required ... ??
            # https://docs.python.org/3/library/asyncio-task.html#asyncio.shield
            # ^ seems as though in combo with the try/except here
            # we're BOLDLY INGORING cancel of the trio fute?
            #
            # I guess it makes sense bc we don't want `asyncio` to
            # cancel trio just because they can't handle SIGINT
            # sanely? XD .. kk

            # XXX, sin-shield causes guest-run abandons on SIGINT..
            # out: Outcome = await trio_done_fute

            # NOTE will raise (via `Error.unwrap()`) from any
            # exception packed into the guest-run's `main_outcome`.
            return out.unwrap()

        except (
            # XXX special SIGINT-handling is required since
            # `asyncio.shield()`-ing seems to NOT handle that case as
            # per recent changes in 3.11:
            # https://docs.python.org/3/library/asyncio-runner.html#handling-keyboard-interruption
            #
            # NOTE: further, apparently ONLY need to handle this
            # special SIGINT case since all other `asyncio`-side
            # errors can be processed via our `chan._aio_err`
            # relaying (right?); SIGINT seems to be totally diff
            # error path in `asyncio`'s runtime..?
            asyncio.CancelledError,

        ) as _fute_err:
            fute_err = _fute_err
            err_message: str = (
                'main `asyncio` task '
                'was cancelled!\n'
            )

            # TODO, handle possible edge cases with
            # `open_root_actor()` closing before this is run!
            #
            actor: tractor.Actor = tractor.current_actor()

            log.exception(
                err_message
                +
                'Cancelling `trio`-side `tractor`-runtime..\n'
                f'c(>\n'
                f'  |_{actor}.cancel_soon()\n'
            )

            # XXX WARNING XXX the next LOCs are super important!
            #
            # SINCE without them, we can get guest-run ABANDONMENT
            # cases where `asyncio` will not schedule or wait on the
            # guest-run `trio.Task` nor invoke its registered
            # `trio_done_callback()` before final shutdown!
            #
            # This is particularly true if the `trio` side has tasks
            # in shielded sections when an OC-cancel (SIGINT)
            # condition occurs!
            #
            # We now have the
            # `test_infected_asyncio.test_sigint_closes_lifetime_stack()`
            # suite to ensure we do not suffer this issues
            # (hopefully) ever again.
            #
            # The original abandonment issue surfaced as 2 different
            # race-condition dependent types scenarios all to do with
            # `asyncio` handling SIGINT from the system:
            #
            # - "silent-abandon" (WORST CASE):
            #  `asyncio` abandons the `trio` guest-run task silently
            #  and no `trio`-guest-run or `tractor`-actor-runtime
            #  teardown happens whatsoever..
            #
            # - "loud-abandon" (BEST-ish CASE):
            #   the guest run get's abaondoned "loudly" with `trio`
            #   reporting a console traceback and further tbs of all
            #   the (failed) GC-triggered shutdown routines which
            #   thankfully does get dumped to console..
            #
            # The abandonment is most easily reproduced if the `trio`
            # side has tasks doing shielded work where those tasks
            # ignore the normal `Cancelled` condition and continue to
            # run, but obviously `asyncio` isn't aware of this and at
            # some point bails on the guest-run unless we take manual
            # intervention..
            #
            # To repeat, *WITHOUT THIS* stuff below the guest-run can
            # get race-conditionally abandoned!!
            #
            # XXX SOLUTION XXX
            # ------ - ------
            # XXX FIRST PART:
            # ------ - ------
            # the obvious fix to the "silent-abandon" case is to
            # explicitly cancel the actor runtime such that no
            # runtime tasks are even left unaware that the guest-run
            # should be terminated due to OS cancellation.
            #
            actor.cancel_soon()

            # ------ - ------
            # XXX SECOND PART:
            # ------ - ------
            # Pump the `asyncio` event-loop to allow
            # `trio`-side to `trio`-guest-run to complete and
            # teardown !!
            #
            # oh `asyncio`, how i don't miss you at all XD
            while not trio_done_fute.done():
                log.runtime(
                    'Waiting on main guest-run `asyncio` task to complete..\n'
                    f'|_trio_done_fut: {trio_done_fute}\n'
                )
                await asyncio.sleep(_sigint_loop_pump_delay)

                # XXX is there any alt API/approach like the internal
                # call below but that doesn't block indefinitely..?
                # loop._run_once()

            try:
                return trio_done_fute.result()
            except (
                asyncio.InvalidStateError,
                # asyncio.CancelledError,
                # ^^XXX `.shield()` call above prevents this??

            )as state_err:

                # XXX be super dupere noisy about abandonment issues!
                aio_task: asyncio.Task = asyncio.current_task()
                message: str = (
                    'The `asyncio`-side task likely exited before the '
                    '`trio`-side guest-run completed!\n\n'
                )
                if fute_err:
                    message += (
                        f'The main {aio_task}\n'
                        f'STOPPED due to {type(fute_err)}\n\n'
                    )

                message += (
                    f'Likely something inside our guest-run-as-task impl is '
                    f'not effectively waiting on the `trio`-side to complete ?!\n'
                    f'This code -> {aio_main!r}\n\n'

                    'Below you will likely see a '
                    '"RuntimeWarning: Trio guest run got abandoned.." !!\n'
                )
                raise AsyncioRuntimeTranslationError(message) from state_err

    # might as well if it's installed.
    try:
        import uvloop
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)
    except ImportError:
        log.runtime('`uvloop` not available..')

    return asyncio.run(
        aio_main(trio_main),
    )
