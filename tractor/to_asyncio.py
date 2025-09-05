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
from asyncio.exceptions import (
    CancelledError,
)
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass
import inspect
import platform
import traceback
from typing import (
    Any,
    Callable,
    AsyncIterator,
    Awaitable,
)

import tractor
from tractor._exceptions import (
    InternalError,
    TrioTaskExited,
    TrioCancelled,
    AsyncioTaskExited,
    AsyncioCancelled,
)
from tractor._state import (
    debug_mode,
    _runtime_vars,
)
from tractor._context import Unresolved
from tractor.devx import debug
from tractor.log import (
    get_logger,
    StackLevelAdapter,
)
# TODO, wite the equiv of `trio.abc.Channel` but without attrs..
# -[ ] `trionics.chan_types.ChanStruct` maybe?
# from tractor.msg import (
#     pretty_struct,
# )
from tractor.trionics import (
    is_multi_cancelled,
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

if (_py_313 := (
        ('3', '13')
        ==
        platform.python_version_tuple()[:-1]
    )
):
    # 3.13+ only.. lel.
    # https://docs.python.org/3.13/library/asyncio-queue.html#asyncio.QueueShutDown
    from asyncio import (
        QueueShutDown,
    )
else:
    QueueShutDown = False


# TODO, generally speaking we can generalize this abstraction, a "SC linked
# parent->child task pair", as the same "supervision scope primitive"
# **that is** our `._context.Context` with the only difference being
# in how the tasks conduct msg-passing comms.
#
# For `LinkedTaskChannel` we are passing the equivalent of (once you
# include all the recently added  `._trio/aio_to_raise`
# exd-as-signals) our SC-dialog-proto over each asyncIO framework's
# mem-chan impl,
#
# verus in `Context`
#
# We are doing the same thing but msg-passing comms happens over an
# IPC transport between tasks in different memory domains.

@dataclass
class LinkedTaskChannel(
    trio.abc.Channel,

    # XXX LAME! meta-base conflict..
    # pretty_struct.Struct,
):
    '''
    A "linked task channel" which allows for two-way synchronized msg
    passing between a ``trio``-in-guest-mode task and an ``asyncio``
    task scheduled in the host loop.

    '''
    _to_aio: asyncio.Queue
    _from_aio: trio.MemoryReceiveChannel

    _to_trio: trio.MemorySendChannel
    _trio_cs: trio.CancelScope
    _trio_task: trio.Task
    _aio_task_complete: trio.Event

    _closed_by_aio_task: bool = False
    _suppress_graceful_exits: bool = True

    _trio_err: BaseException|None = None
    _trio_to_raise: (
        AsyncioTaskExited|  # aio task exits while trio ongoing
        AsyncioCancelled|  # aio task is (self-)cancelled
        BaseException|
        None
    ) = None
    _trio_exited: bool = False

    # set after `asyncio.create_task()`
    _aio_task: asyncio.Task|None = None
    _aio_err: BaseException|None = None
    _aio_to_raise: (
        TrioTaskExited|  # trio task exits while aio ongoing
        BaseException|
        None
    ) = None
    # _aio_first: Any|None = None  # TODO?
    _aio_result: Any|Unresolved = Unresolved

    def _final_result_is_set(self) -> bool:
        return self._aio_result is not Unresolved

    # TODO? equiv from `Context`?
    # @property
    # def has_outcome(self) -> bool:
    #     return (
    #         bool(self.maybe_error)
    #         or
    #         self._final_result_is_set()
    #     )

    async def wait_for_result(
        self,
        hide_tb: bool = True,

    ) -> Any:
        '''
        Wait for the `asyncio.Task.result()` from `trio`

        '''
        __tracebackhide__: bool = hide_tb
        assert self._portal, (
            '`Context.wait_for_result()` can not be called from callee side!'
        )
        if self._final_result_is_set():
            return self._aio_result

        async with translate_aio_errors(
            chan=self,
            wait_aio_task=False,
        ):
            await self._aio_task_complete.wait()

        if (
            not self._final_result_is_set()
        ):
            if (trio_to_raise := self._trio_to_raise):
                raise trio_to_raise from self._aio_err

            elif aio_err := self._aio_err:
                raise aio_err

            else:
                raise InternalError(
                    f'Asyncio-task has no result or error set !?\n'
                    f'{self._aio_task}'
                )

        return self._aio_result

    _broadcaster: BroadcastReceiver|None = None

    async def aclose(self) -> None:
        await self._from_aio.aclose()

    # ?TODO? async version of this?
    def started_nowait(
        self,
        val: Any = None,
    ) -> None:
        '''
        Synchronize aio-side with its trio-parent.

        '''
        self._aio_started_val = val
        return self._to_trio.send_nowait(val)

    # TODO, mk this side-agnostic?
    #
    # -[ ] add private meths for both sides and dynamically
    #    determine which to use based on task-type read at calltime?
    #    -[ ] `._recv_trio()`: receive to trio<-asyncio
    #    -[ ] `._send_trio()`: send from trio->asyncio
    #    -[ ] `._recv_aio()`: send from asyncio->trio
    #    -[ ] `._send_aio()`: receive to asyncio<-trio
    #
    # -[ ] pass the instance to the aio side instead of the separate
    #    per-side chan types?
    #
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
                chan=self,
                # NOTE, determined by `open_channel_from()` input arg
                suppress_graceful_exits=self._suppress_graceful_exits,

                # XXX: obviously this will deadlock if an on-going stream is
                # being procesed.
                # wait_on_aio_task=False,
            ):
                raise err

    async def send(self, item: Any) -> None:
        '''
        Send a value through to the asyncio task presuming
        it defines a ``from_trio`` argument, if it does not
        this method will raise an error.

        '''
        self._to_aio.put_nowait(item)

    # TODO? needed?
    # async def wait_aio_complete(self) -> None:
    #     await self._aio_task_complete.wait()

    def cancel_asyncio_task(
        self,
        msg: str = '',
    ) -> None:
        self._aio_task.cancel(
            msg=msg,
        )

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
    suppress_graceful_exits: bool = True,
    hide_tb: bool = True,
    **kwargs,

) -> LinkedTaskChannel:
    '''
    Run an `asyncio`-compat async function or generator in a task,
    return or stream the result back to the caller
    `trio.lowleve.Task`.

    '''
    __tracebackhide__: bool = hide_tb
    if not (actor := tractor.current_actor()).is_infected_aio():
        raise RuntimeError(
            f'`infect_asyncio: bool` mode is not enabled ??\n'
            f'Ensure you pass `ActorNursery.start_actor(infect_asyncio=True)`\n'
            f'\n'
            f'{actor}\n'
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

    trio_task: trio.Task = trio.lowlevel.current_task()
    trio_cs = trio.CancelScope()
    aio_task_complete = trio.Event()

    chan = LinkedTaskChannel(
        _to_aio=aio_q,  # asyncio.Queue
        _from_aio=from_aio,  # recv chan
        _to_trio=to_trio,  # send chan
        _trio_cs=trio_cs,
        _trio_task=trio_task,
        _aio_task_complete=aio_task_complete,
        _suppress_graceful_exits=suppress_graceful_exits,
    )

    # allow target func to accept/stream results manually by name
    if 'to_trio' in args:
        kwargs['to_trio'] = to_trio

    if 'from_trio' in args:
        kwargs['from_trio'] = from_trio

    if 'chan' in args:
        kwargs['chan'] = chan

    if provide_channels:
        assert (
            'to_trio' in args
            or
            'chan' in args
        )

    coro = func(**kwargs)

    async def wait_on_coro_final_result(
        to_trio: trio.MemorySendChannel,
        coro: Awaitable,
        aio_task_complete: trio.Event,

    ) -> None:
        '''
        Await input `coro` as/in an `asyncio.Task` and deliver final
        `return`-ed result back to `trio`.

        '''
        nonlocal chan

        orig = result = id(coro)
        try:
            result: Any = await coro
            chan._aio_result = result
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
                chan._aio_err is None
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
                # breakpoint()  # TODO! why no work!?
                # import pdbp; pdbp.set_trace()

                # IFF there is a blocked trio waiter, we set the
                # aio-side error to be an explicit "exited early"
                # (much like a `Return` in our SC IPC proto) for the
                # `.open_channel_from()` case where the parent trio
                # task might not wait directly for a final returned
                # result (i.e. the trio side might be waiting on
                # a streamed value) - this is a signal that the
                # asyncio.Task has returned early!
                #
                # TODO, solve other cases where trio side might,
                # - raise Cancelled but aio side exits on next tick.
                # - raise error but aio side exits on next tick.
                # - raise error and aio side errors "independently"
                #   on next tick (SEE draft HANDLER BELOW).
                stats: trio.MemoryChannelStatistics = to_trio.statistics()
                if (
                    stats.tasks_waiting_receive
                    and
                    not chan._aio_err
                ):
                    chan._trio_to_raise = AsyncioTaskExited(
                        f'Task exited with final result: {result!r}\n'
                    )

                # XXX ALWAYS close the child-`asyncio`-task-side's
                # `to_trio` handle which will in turn relay
                # a `trio.EndOfChannel` to the `trio`-parent.
                # Consequently the parent `trio` task MUST ALWAYS
                # check for any `chan._aio_err` to be raised when it
                # receives an EoC.
                #
                # NOTE, there are 2 EoC cases,
                # - normal/graceful EoC due to the aio-side actually
                #   terminating its "streaming", but the task did not
                #   error and is not yet complete.
                #
                # - the aio-task terminated and we specially mark the
                #   closure as due to the `asyncio.Task`'s exit.
                #
                to_trio.close()
                chan._closed_by_aio_task = True

            aio_task_complete.set()
            log.runtime(
                f'`asyncio` task completed\n'
                f')>\n'
                f' |_{task}\n'
            )

    # start the asyncio task we submitted from trio
    if not inspect.isawaitable(coro):
        raise TypeError(
            f'Pass the async-fn NOT a coroutine\n'
            f'{coro!r}'
        )

    # schedule the (bg) `asyncio.Task`
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
        (greenback := debug.maybe_import_greenback(
            force_reload=True,
            raise_not_found=False,
        ))
    ):
        log.devx(
            f'Bestowing `greenback` portal for `asyncio`-task\n'
            f'{task}\n'
        )
        greenback.bestow_portal(task)

    def signal_trio_when_done(
        task: asyncio.Task,
    ) -> None:
        '''
        Maybe-cancel, relay-and-raise an error to, OR pack a final
        `return`-value for the parent (in SC terms) `trio.Task` on
        completion of the `asyncio.Task`.

        Note for certain "edge" scheduling-race-conditions we allow
        the aio side to dictate dedicated `tractor`-defined excs to
        be raised in the `trio` parent task; the intention is to
        indicate those races in a VERY pedantic manner!

        '''
        nonlocal chan
        trio_err: BaseException|None = chan._trio_err

        # XXX, since the original error we read from the asyncio.Task
        # might change between BEFORE and AFTER we here call
        # `asyncio.Task.result()`
        #
        # -> THIS is DUE TO US in `translate_aio_errors()`!
        #
        # => for example we might set a special exc
        # (`AsyncioCancelled|AsyncioTaskExited`) meant to be raised
        # in trio (and maybe absorbed depending on the called API)
        # BEFORE this done-callback is invoked by `asyncio`'s
        # runtime.
        trio_to_raise: BaseException|None = chan._trio_to_raise
        orig_aio_err: BaseException|None = chan._aio_err
        aio_err: BaseException|None = None

        # only to avoid `asyncio` complaining about uncaptured
        # task exceptions
        try:
            res: Any = task.result()
            log.info(
                f'`trio` received final result from `asyncio` task,\n'
                f')> {res}\n'
                f' |_{task}\n'
            )
            if not chan._aio_result:
                chan._aio_result = res

            # ?TODO, should we also raise `AsyncioTaskExited[res]`
            # in any case where trio is NOT blocking on the
            # `._to_trio` chan?
            #
            # -> ?NO RIGHT? since the
            #   `open_channel_from().__aexit__()` should detect this
            #   and then set any final `res` from above as a field
            #   that can optionally be read by the trio-paren-task as
            #   needed (just like in our
            #   `Context.wait_for_result()/.result` API yah?
            #
            # if provide_channels:

        except BaseException as _aio_err:
            aio_err: BaseException = _aio_err

            # READ AGAIN, AFTER the `asyncio` side errors, in case
            # it was cancelled due to an error from `trio` (or
            # some other out of band exc) and then set to something
            # else?
            curr_aio_err: BaseException|None = chan._aio_err

            # always true right?
            assert (
                type(aio_err)
                is type(orig_aio_err)
                is type(curr_aio_err)
            ), (
                f'`asyncio`-side task errors mismatch?!?\n\n'
                f'(caught) aio_err: {aio_err}\n'
                f'ORIG chan._aio_err: {orig_aio_err}\n'
                f'chan._aio_err: {curr_aio_err}\n'
            )

            msg: str = (
                '`trio`-side reports that the `asyncio`-side '
                '{etype_str}\n'
                # ^NOTE filled in below
            )
            if isinstance(aio_err, CancelledError):
                msg += (
                    f'c)>\n'
                    f' |_{task}\n'
                )
                log.cancel(
                    msg.format(etype_str='cancelled')
                )

            # XXX when the asyncio.Task exits early (before the trio
            # side) we relay through an exc-as-signal which is
            # normally suppressed unless the trio.Task also errors
            #
            # ?TODO, is this even needed (does it happen) now?
            elif (
                _py_313
                and
                isinstance(aio_err, QueueShutDown)
            ):
                # import pdbp; pdbp.set_trace()
                trio_err = AsyncioTaskExited(
                    'Task exited before `trio` side'
                )
                if not chan._trio_err:
                    chan._trio_err = trio_err

                msg += (
                    f')>\n'
                    f' |_{task}\n'
                )
                log.info(
                    msg.format(etype_str='exited')
                )

            else:
                msg += (
                    f'x)>\n'
                    f' |_{task}\n'
                )
                log.exception(
                    msg.format(etype_str='errored')
                )

        # is trio the src of the aio task's exc-as-outcome?
        trio_err: BaseException|None = chan._trio_err
        curr_aio_err: BaseException|None = chan._aio_err
        if (
            curr_aio_err
            or
            trio_err
            or
            trio_to_raise
        ):
            # XXX, if not already, ALWAYs cancel the trio-side on an
            # aio-side error or early return. In the case where the trio task is
            # blocking on a checkpoint or `asyncio.Queue.get()`.

            # NOTE: currently mem chan closure may act as a form
            # of error relay (at least in the `asyncio.CancelledError`
            # case) since we have no way to directly trigger a `trio`
            # task error without creating a nursery to throw one.
            # We might want to change this in the future though.
            from_aio.close()

            if (
                not trio_cs.cancelled_caught
                or
                not trio_cs.cancel_called
            ):
                log.cancel(
                    f'Cancelling trio-side due to aio-side src exc\n'
                    f'\n'
                    f'{curr_aio_err!r}\n'
                    f'\n'
                    f'(c>\n'
                    f'  |_{trio_task}\n'
                )
                trio_cs.cancel()

            # maybe the `trio` task errored independent from the
            # `asyncio` one and likely in between
            # a guest-run-sched-tick.
            #
            # The obvious ex. is where one side errors during
            # the current tick and then the other side immediately
            # errors before its next checkpoint; i.e. the 2 errors
            # are "independent".
            #
            # "Independent" here means in the sense that neither task
            # was the explicit cause of the other side's exception
            # according to our `tractor.to_asyncio` SC API's error
            # relaying mechanism(s); the error pair is *possibly
            # due-to* but **not necessarily** inter-related by some
            # (subsys) state between the tasks,
            #
            # NOTE, also see the `test_trio_prestarted_task_bubbles`
            # for reproducing detailed edge cases as per the above
            # cases.
            #
            trio_to_raise: AsyncioCancelled|AsyncioTaskExited = chan._trio_to_raise
            aio_to_raise: TrioTaskExited|TrioCancelled = chan._aio_to_raise
            if (
                not chan._aio_result
                and
                not trio_cs.cancelled_caught
                and (
                    (aio_err and type(aio_err) not in {
                        asyncio.CancelledError
                    })
                    or
                    aio_to_raise
                )
                and (
                    ((trio_err := chan._trio_err) and type(trio_err) not in {
                        trio.Cancelled,
                    })
                    or
                    trio_to_raise
                )
            ):
                eg = ExceptionGroup(
                    'Both the `trio` and `asyncio` tasks errored independently!!\n',
                    (
                        trio_to_raise or trio_err,
                        aio_to_raise or aio_err,
                    ),
                )
                # chan._trio_err = eg
                # chan._aio_err = eg
                raise eg

            elif aio_err:
                # XXX raise any `asyncio` side error IFF it doesn't
                # match the one we just caught from the task above!
                # (that would indicate something weird/very-wrong
                # going on?)
                if (
                    aio_err is not trio_to_raise
                    and (
                        not suppress_graceful_exits
                        and (
                            chan._aio_result is not Unresolved
                            and
                            isinstance(trio_to_raise, AsyncioTaskExited)
                        )
                    )
                ):
                    # raise aio_err from relayed_aio_err
                    raise trio_to_raise from curr_aio_err

                raise aio_err

    task.add_done_callback(signal_trio_when_done)
    return chan


@acm
async def translate_aio_errors(
    chan: LinkedTaskChannel,
    wait_on_aio_task: bool = False,
    cancel_aio_task_on_trio_exit: bool = True,
    suppress_graceful_exits: bool = True,

    hide_tb: bool = True,

) -> AsyncIterator[None]:
    '''
    An error handling to cross-loop propagation context around
    `asyncio.Task` spawns via one of this module's APIs:

    - `open_channel_from()`
    - `run_task()`

    appropriately translates errors and cancels into ``trio`` land.

    '''
    __tracebackhide__: bool = hide_tb

    trio_task = trio.lowlevel.current_task()
    aio_err: BaseException|None = chan._aio_err
    aio_task: asyncio.Task = chan._aio_task
    aio_done_before_trio: bool = aio_task.done()
    assert aio_task
    trio_err: BaseException|None = None
    eoc: trio.EndOfChannel|None = None
    try:
        yield  # back to one of the cross-loop apis
    except trio.Cancelled as taskc:
        trio_err = taskc
        chan._trio_err = trio_err

        # should NEVER be the case that `trio` is cancel-handling
        # BEFORE the other side's task-ref was set!?
        assert chan._aio_task

        # import pdbp; pdbp.set_trace()  # lolevel-debug

        # relay cancel through to called `asyncio` task
        chan._aio_to_raise = TrioCancelled(
            f'trio`-side cancelled the `asyncio`-side,\n'
            f'c)>\n'
            f'  |_{trio_task}\n'
            f'\n'
            f'trio src exc: {trio_err!r}\n'
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

    # XXX EoC is a special SIGNAL from the aio-side here!
    # There are 2 cases to handle:
    # 1. the "EoC passthrough" case.
    #   - the aio-task actually closed the channel "gracefully" and
    #     the trio-task should unwind any ongoing channel
    #     iteration/receiving,
    #  |_this exc-translator wraps calls to `LinkedTaskChannel.receive()`
    #    in which case we want to relay the actual "end-of-chan" for
    #    iteration purposes.
    #
    # 2. relaying the "asyncio.Task termination" case.
    #   - if the aio-task terminates, maybe with an error, AND the
    #    `open_channel_from()` API was used, it will always signal
    #    that termination.
    #  |_`wait_on_coro_final_result()` always calls
    #    `to_trio.close()` when `provide_channels=True` so we need to
    #    always check if there is an aio-side exc which needs to be
    #    relayed to the parent trio side!
    #  |_in this case the special `chan._closed_by_aio_task` is
    #    ALWAYS set.
    #
    except trio.EndOfChannel as _eoc:
        eoc = _eoc
        if (
            chan._closed_by_aio_task
            and
            aio_err
        ):
            log.cancel(
                f'The asyncio-child task terminated due to error\n'
                f'{aio_err!r}\n'
            )
            chan._trio_to_raise = aio_err
            trio_err = chan._trio_err = eoc
            #
            # ?TODO?, raise something like a,
            # chan._trio_to_raise = AsyncioErrored()
            # BUT, with the tb rewritten to reflect the underlying
            # call stack?
        else:
            trio_err = chan._trio_err = eoc

        raise eoc

    # NOTE ALSO SEE the matching note in the `cancel_trio()` asyncio
    # task-done-callback.
    #
    # when the aio side is (possibly self-)cancelled it will close
    # the `chan._to_trio` and thus trigger the trio side to raise
    # a dedicated `AsyncioCancelled`
    except (
        trio.ClosedResourceError,
    ) as cre:
        chan._trio_err = cre
        aio_err = chan._aio_err

        # XXX if an underlying `asyncio.CancelledError` triggered
        # this channel close, raise our (non-`BaseException`) wrapper
        # exception (`AsyncioCancelled`) from that source error.
        if (
            # aio-side is cancelled?
            # |_ first not set until it terminates??
            aio_task.cancelled()
            and
            type(aio_err) is CancelledError

            # TODO, if we want suppression of the
            # silent-exit-by-`trio` case?
            # -[ ] the parent task can also just catch it though?
            # -[ ] OR, offer a `signal_aio_side_on_exit=True` ??
        ):
            # await tractor.pause(shield=True)
            chan._trio_to_raise = AsyncioCancelled(
                f'asyncio`-side cancelled the `trio`-side,\n'
                f'c(>\n'
                f'  |_{aio_task}\n\n'

                f'(triggered on the `trio`-side by a {cre!r})\n'
            )
            # TODO?? needed or does this just get reraised in the
            # `finally:` block below?
            # raise to_raise_trio from aio_err

        # maybe the chan-closure is due to something else?
        else:
            raise cre

    except BaseException as _trio_err:
        trio_err = chan._trio_err = _trio_err
        # await tractor.pause(shield=True)  # workx!
        entered: bool = await debug._maybe_enter_pm(
            trio_err,
            api_frame=inspect.currentframe(),
        )
        if (
            not entered
            and
            not is_multi_cancelled(trio_err)
        ):
            log.exception(
                '`trio`-side task errored?'
            )
            # __tracebackhide__: bool = False

        # TODO, just a log msg here indicating the scope closed
        # and that the trio-side expects that and what the final
        # result from the aio side was?
        #
        # if isinstance(chan._aio_err, AsyncioTaskExited):
        #     await tractor.pause(shield=True)

        # if aio side is still active cancel it due to the trio-side
        # error!
        # ?TODO, mk `AsyncioCancelled[typeof(trio_err)]` embed the
        # current exc?
        if (
            # not aio_task.cancelled()
            # and
            not aio_task.done()  # TODO? only need this one?

            # XXX LOL, so if it's not set it's an error !?
            # yet another good jerb by `ascyncio`..
            # and
            # not aio_task.exception()
        ):
            aio_taskc = TrioCancelled(
                f'The `trio`-side task crashed!\n'
                f'{trio_err}'
            )
            # ??TODO? move this into the func that tries to use
            # `Task._fut_waiter: Future` instead??
            #
            # aio_task.set_exception(aio_taskc)
            # wait_on_aio_task = False
            try:
                aio_task.set_exception(aio_taskc)
            except (
                asyncio.InvalidStateError,
                RuntimeError,
                # ^XXX, uhh bc apparently we can't use `.set_exception()`
                # any more XD .. ??
            ):
                wait_on_aio_task = False

    finally:
        # record wtv `trio`-side error transpired
        if trio_err:
            assert chan._trio_err is trio_err
            # if chan._trio_err is not trio_err:
            #     await tractor.pause(shield=True)

        ya_trio_exited: bool = chan._trio_exited
        graceful_trio_exit: bool = (
            ya_trio_exited
            and
            not chan._trio_err   # XXX CRITICAL, `asyncio.Task.cancel()` is cucked man..
        )

        # XXX NOTE! XXX by default always cancel the `asyncio` task if
        # we've made it this far and it's not done.
        # TODO, how to detect if there's an out-of-band error that
        # caused the exit?
        if (
            not aio_task.done()
            and (
                cancel_aio_task_on_trio_exit
                # and
                # chan._aio_err  # TODO, if it's not .done() is this possible?

            # did the `.open_channel_from()` parent caller already
            # (gracefully) exit scope before this translator was
            # invoked?
            # => since we couple the lifetime of the `asyncio.Task`
            #   to the `trio` parent task, it should should also be
            #   terminated via either,
            #
            #   1. raising an explicit `TrioTaskExited|TrioCancelled`
            #      in task via `asyncio.Task._fut_waiter.set_exception()`
            #
            #   2. or (worst case) by cancelling the aio task using
            #      the std-but-never-working `asyncio.Task.cancel()`
            #      (which i can't figure out why that nor
            #      `Task.set_exception()` seem to never ever do the
            #      rignt thing! XD).
                or
                graceful_trio_exit
            )
        ):
            report: str = (
                'trio-side exited silently!'
            )
            assert not chan._aio_err, (
                'WTF why duz asyncio have err but not dun?!'
            )

            # if the `trio.Task` terminated without raising
            # `trio.Cancelled` (curently handled above) there's
            # 2 posibilities,
            #
            # i. it raised a `trio_err`
            # ii. it did a "silent exit" where the
            #    `open_channel_from().__aexit__()` phase ran without
            #    any raise or taskc (task cancel) and no final result
            #    was collected (yet) from the aio side.
            #
            # SO, ensure the asyncio-side is notified and terminated
            # by a dedicated exc-as-signal which distinguishes
            # various aio-task-state at termination cases.
            #
            # Consequently if the aio task doesn't absorb said
            # exc-as-signal, the trio side should then see the same exc
            # propagate up through the .open_channel_from() call to
            # the parent task.
            #
            # if the `trio.Task` already exited (only can happen for
            # the `open_channel_from()` use case) block due to to
            # either plain ol' graceful `__aexit__()` or due to taskc
            # or an error, we ensure the aio-side gets signalled via
            # an explicit exception and its `Queue` is shutdown.
            if ya_trio_exited:
                # XXX py3.13+ ONLY..
                # raise `QueueShutDown` on next `Queue.get/put()`
                if _py_313:
                    chan._to_aio.shutdown()

                # pump this event-loop (well `Runner` but ya)
                #
                # TODO? is this actually needed?
                # -[ ] theory is this let's the aio side error on
                #     next tick and then we sync task states from
                #     here onward?
                await trio.lowlevel.checkpoint()

                # TODO? factor the next 2 branches into a func like
                # `try_terminate_aio_task()` and use it for the taskc
                # case above as well?
                fut: asyncio.Future|None = aio_task._fut_waiter
                if (
                    fut
                    and
                    not fut.done()
                ):
                    # await tractor.pause()
                    if graceful_trio_exit:
                        fut.set_exception(
                            TrioTaskExited(
                                f'the `trio.Task` gracefully exited but '
                                f'its `asyncio` peer is not done?\n'
                                f')>\n'
                                f' |_{trio_task}\n'
                                f'\n'
                                f'>>\n'
                                f' |_{aio_task!r}\n'
                            )
                        )

                    # TODO? should this need to exist given the equiv
                    # `TrioCancelled` equivalent in the be handler
                    # above??
                    else:
                        fut.set_exception(
                            TrioTaskExited(
                                f'The `trio`-side task crashed!\n'
                                f'{trio_err}'
                            )
                        )
                else:
                    aio_taskc_warn: str = (
                        f'\n'
                        f'MANUALLY Cancelling `asyncio`-task: {aio_task.get_name()}!\n\n'
                        f'**THIS CAN SILENTLY SUPPRESS ERRORS FYI\n\n'
                    )
                    # await tractor.pause()
                    report += aio_taskc_warn
                    # TODO XXX, figure out the case where calling this makes the
                    # `test_infected_asyncio.py::test_trio_closes_early_and_channel_exits`
                    # hang and then don't call it in that case!
                    #
                    aio_task.cancel(msg=aio_taskc_warn)

            log.warning(report)

        # sync with the `asyncio.Task`'s completion to ensure any
        # error is captured and relayed (via
        # `channel._aio_err/._trio_to_raise`) BEFORE calling
        # `maybe_raise_aio_side_err()` below!
        #
        # XXX WARNING NOTE
        # the `task.set_exception(aio_taskc)` call above MUST NOT
        # EXCEPT or this WILL HANG!! SO, if you get a hang maybe step
        # through and figure out why it erroed out up there!
        #
        if wait_on_aio_task:
            await chan._aio_task_complete.wait()
            log.debug(
                'asyncio-task is done and unblocked trio-side!\n'
            )

        # NOTE, was a `maybe_raise_aio_side_err()` closure that
        # i moved inline BP
        '''
        Raise any `trio`-side-caused cancellation or legit task
        error normally propagated from the caller of either,
          - `open_channel_from()`
          - `run_task()`

        '''
        aio_err: BaseException|None = chan._aio_err
        trio_to_raise: (
            AsyncioCancelled|
            AsyncioTaskExited|
            Exception|  # relayed from aio-task
            None
        ) = chan._trio_to_raise

        raise_from: Exception = (
            trio_err if (aio_err is trio_to_raise)
            else aio_err
        )

        if not suppress_graceful_exits:
            raise trio_to_raise from raise_from

        if trio_to_raise:
            match (
                trio_to_raise,
                trio_err,
            ):
                case (
                    AsyncioTaskExited(),
                    trio.Cancelled()|
                    None,
                ):
                    log.info(
                        'Ignoring aio exit signal since trio also exited!'
                    )
                    return

                case (
                    AsyncioTaskExited(),
                    trio.EndOfChannel(),
                ):
                    raise trio_err

                case (
                    AsyncioCancelled(),
                    trio.Cancelled(),
                ):
                    if not aio_done_before_trio:
                        log.info(
                            'Ignoring aio cancelled signal since trio was also cancelled!'
                        )
                        return
                case _:
                    raise trio_to_raise from raise_from

        # Check if the asyncio-side is the cause of the trio-side
        # error.
        elif (
            aio_err is not None
            and
            type(aio_err) is not AsyncioCancelled
        ):
            # always raise from any captured asyncio error
            if trio_err:
                raise trio_err from aio_err

            # XXX NOTE! above in the `trio.ClosedResourceError`
            # handler we specifically set the
            # `aio_err = AsyncioCancelled` such that it is raised
            # as that special exc here!
            raise aio_err

        if trio_err:
            raise trio_err

        # ^^TODO?? case where trio_err is not None and
        # aio_err is AsyncioTaskExited => raise eg!
        # -[x] maybe use a match bc this get's real
        #     complex fast XD
        #  => i did this above for silent exit cases ya?


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
            suppress_graceful_exits=chan._suppress_graceful_exits,
        ):
            # return single value that is the output from the
            # ``asyncio`` function-as-task. Expect the mem chan api
            # to do the job of handling cross-framework cancellations
            # / errors via closure and translation in the
            # `translate_aio_errors()` in the above ctx mngr.

            return await chan._from_aio.receive()
            # return await chan.receive()


@acm
async def open_channel_from(
    target: Callable[..., Any],
    suppress_graceful_exits: bool = True,
    **target_kwargs,

) -> AsyncIterator[Any]:
    '''
    Open an inter-loop linked task channel for streaming between a target
    spawned ``asyncio`` task and ``trio``.

    '''
    chan: LinkedTaskChannel = _run_asyncio_task(
        target,
        qsize=2**8,
        provide_channels=True,
        suppress_graceful_exits=suppress_graceful_exits,
        **target_kwargs,
    )
    # TODO, tuple form here?
    async with chan._from_aio:
        async with translate_aio_errors(
            chan,
            wait_on_aio_task=True,
            suppress_graceful_exits=suppress_graceful_exits,
        ):
            # sync to a "started()"-like first delivered value from the
            # ``asyncio`` task.
            try:
                with (cs := chan._trio_cs):
                    first = await chan.receive()

                    # deliver stream handle upward
                    yield first, chan
            except trio.Cancelled as taskc:
                if cs.cancel_called:
                    if isinstance(chan._trio_to_raise, AsyncioCancelled):
                        log.cancel(
                            f'trio-side was manually cancelled by aio side\n'
                            f'|_c>}}{cs!r}?\n'
                        )
                    # TODO, maybe a special `TrioCancelled`???

                raise taskc

            finally:
                chan._trio_exited = True

                # when the aio side is still ongoing but trio exits
                # early we signal with a special exc (kinda like
                # a `Return`-msg for IPC ctxs)
                aio_task: asyncio.Task = chan._aio_task
                if not aio_task.done():
                    fut: asyncio.Future|None = aio_task._fut_waiter
                    if fut:
                        fut.set_exception(
                            TrioTaskExited(
                                f'but the child `asyncio` task is still running?\n'
                                f'>>\n'
                                f' |_{aio_task!r}\n'
                            )
                        )
                    else:
                        # XXX SHOULD NEVER HAPPEN!
                        await tractor.pause()
                else:
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
            # await debug.maybe_init_greenback(
            #     force_reload=True,
            # )

        def trio_done_callback(main_outcome: Outcome):
            log.runtime(
                f'`trio` guest-run finishing with outcome\n'
                f'>) {main_outcome}\n'
                f'|_{trio_done_fute}\n'
            )

            # import pdbp; pdbp.set_trace()
            if isinstance(main_outcome, Error):
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

        # XXX, should never get here ;)
        # else:
        #     import pdbp; pdbp.set_trace()

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
