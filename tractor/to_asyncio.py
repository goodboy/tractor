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
from tractor._exceptions import AsyncioCancelled
from tractor._state import (
    debug_mode,
)
from tractor.devx import _debug
from tractor.log import get_logger
from tractor.trionics._broadcast import (
    broadcast_receiver,
    BroadcastReceiver,
)
import trio
from outcome import (
    Error,
    Outcome,
)

log = get_logger(__name__)


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
    _trio_exited: bool = False

    # set after ``asyncio.create_task()``
    _aio_task: asyncio.Task|None = None
    _aio_err: BaseException|None = None
    _broadcaster: BroadcastReceiver|None = None

    async def aclose(self) -> None:
        await self._from_aio.aclose()

    async def receive(self) -> Any:
        async with translate_aio_errors(
            self,

            # XXX: obviously this will deadlock if an on-going stream is
            # being procesed.
            # wait_on_aio_task=False,
        ):

            # TODO: do we need this to guarantee asyncio code get's
            # cancelled in the case where the trio side somehow creates
            # a state where the asyncio cycle-task isn't getting the
            # cancel request sent by (in theory) the last checkpoint
            # cycle on the trio side?
            # await trio.lowlevel.checkpoint()

            return await self._from_aio.receive()

    async def wait_asyncio_complete(self) -> None:
        await self._aio_task_complete.wait()

    # def cancel_asyncio_task(self) -> None:
    #     self._aio_task.cancel()

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
    Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to the caller `trio.lowleve.Task`.

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
        aio_q,  # asyncio.Queue
        from_aio,  # recv chan
        to_trio,  # send chan

        cancel_scope,
        aio_task_complete,
    )

    async def wait_on_coro_final_result(

        to_trio: trio.MemorySendChannel,
        coro: Awaitable,
        aio_task_complete: trio.Event,

    ) -> None:
        '''
        Await ``coro`` and relay result back to ``trio``.

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
                result != orig and
                aio_err is None and

                # in the ``open_channel_from()`` case we don't
                # relay through the "return value".
                not provide_channels
            ):
                to_trio.send_nowait(result)

        finally:
            # if the task was spawned using ``open_channel_from()``
            # then we close the channels on exit.
            if provide_channels:
                # only close the sender side which will relay
                # a ``trio.EndOfChannel`` to the trio (consumer) side.
                to_trio.close()

            aio_task_complete.set()
            log.runtime(f'`asyncio` task: {task.get_name()} is complete')

    # start the asyncio task we submitted from trio
    if not inspect.isawaitable(coro):
        raise TypeError(f"No support for invoking {coro}")

    task: asyncio.Task = asyncio.create_task(
        wait_on_coro_final_result(
            to_trio,
            coro,
            aio_task_complete
        )
    )
    chan._aio_task: asyncio.Task = task

    # XXX TODO XXX get this actually workin.. XD
    # maybe setup `greenback` for `asyncio`-side task REPLing
    if (
        debug_mode()
        and
        (greenback := _debug.maybe_import_greenback(
            force_reload=True,
            raise_not_found=False,
        ))
    ):
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
        except BaseException as terr:
            task_err: BaseException = terr

            msg: str = (
                'Infected `asyncio` task {etype_str}\n'
                f'|_{task}\n'
            )
            if isinstance(terr, CancelledError):
                log.cancel(
                    msg.format(etype_str='cancelled')
                )
            else:
                log.exception(
                    msg.format(etype_str='cancelled')
                )

            assert type(terr) is type(aio_err), (
                '`asyncio` task error mismatch?!?'
            )

        if aio_err is not None:
            # XXX: uhh is this true?
            # assert task_err, f'Asyncio task {task.get_name()} discrepancy!?'

            # NOTE: currently mem chan closure may act as a form
            # of error relay (at least in the ``asyncio.CancelledError``
            # case) since we have no way to directly trigger a ``trio``
            # task error without creating a nursery to throw one.
            # We might want to change this in the future though.
            from_aio.close()

            if task_err is None:
                assert aio_err
                # wait, wut?
                # aio_err.with_traceback(aio_err.__traceback__)

            # TODO: show when cancellation originated
            # from each side more pedantically?
            # elif (
            #     type(aio_err) is CancelledError
            #     and  # trio was the cause?
            #     cancel_scope.cancel_called
            # ):
            #     log.cancel(
            #         'infected task was cancelled by `trio`-side'
            #     )
            #     raise aio_err from task_err

            # XXX: if not already, alway cancel the scope
            # on a task error in case the trio task is blocking on
            # a checkpoint.
            cancel_scope.cancel()

            if (
                task_err
                and
                aio_err is not task_err
            ):
                raise aio_err from task_err

            # raise any `asyncio` side error.
            raise aio_err

        log.info(
            '`trio` received final result from {task}\n'
            f'|_{res}\n'
        )
        # TODO: do we need this?
        # if task_err:
        #     cancel_scope.cancel()
        #     raise task_err

    task.add_done_callback(cancel_trio)
    return chan


@acm
async def translate_aio_errors(

    chan: LinkedTaskChannel,
    wait_on_aio_task: bool = False,

) -> AsyncIterator[None]:
    '''
    Error handling context around ``asyncio`` task spawns which
    appropriately translates errors and cancels into ``trio`` land.

    '''
    trio_task = trio.lowlevel.current_task()

    aio_err: BaseException|None = None

    # TODO: make thisi a channel method?
    def maybe_raise_aio_err(
        err: Exception|None = None
    ) -> None:
        aio_err = chan._aio_err
        if (
            aio_err is not None
            and
            # not isinstance(aio_err, CancelledError)
            type(aio_err) != CancelledError
        ):
            # always raise from any captured asyncio error
            if err:
                raise aio_err from err
            else:
                raise aio_err

    task = chan._aio_task
    assert task
    try:
        yield

    except (
        trio.Cancelled,
    ):
        # relay cancel through to called ``asyncio`` task
        assert chan._aio_task
        chan._aio_task.cancel(
            msg=f'the `trio` caller task was cancelled: {trio_task.name}'
        )
        raise

    except (
        # NOTE: see the note in the ``cancel_trio()`` asyncio task
        # termination callback
        trio.ClosedResourceError,
        # trio.BrokenResourceError,
    ):
        aio_err = chan._aio_err
        if (
            task.cancelled()
            and
            type(aio_err) is CancelledError
        ):
            # if an underlying `asyncio.CancelledError` triggered this
            # channel close, raise our (non-``BaseException``) wrapper
            # error: ``AsyncioCancelled`` from that source error.
            raise AsyncioCancelled(
                f'Task cancelled\n'
                f'|_{task}\n'
            ) from aio_err

        else:
            raise

    finally:
        if (
            # NOTE: always cancel the ``asyncio`` task if we've made it
            # this far and it's not done.
            not task.done() and aio_err

            # or the trio side has exited it's surrounding cancel scope
            # indicating the lifetime of the ``asyncio``-side task
            # should also be terminated.
            or chan._trio_exited
        ):
            log.runtime(
                f'Cancelling `asyncio`-task: {task.get_name()}'
            )
            # assert not aio_err, 'WTF how did asyncio do this?!'
            task.cancel()

        # Required to sync with the far end ``asyncio``-task to ensure
        # any error is captured (via monkeypatching the
        # ``channel._aio_err``) before calling ``maybe_raise_aio_err()``
        # below!
        if wait_on_aio_task:
            await chan._aio_task_complete.wait()

        # NOTE: if any ``asyncio`` error was caught, raise it here inline
        # here in the ``trio`` task
        maybe_raise_aio_err()


async def run_task(
    func: Callable,
    *,

    qsize: int = 2**10,
    **kwargs,

) -> Any:
    '''
    Run an `asyncio` async function or generator in a task, return
    or stream the result back to `trio`.

    '''
    # simple async func
    chan = _run_asyncio_task(
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
    chan = _run_asyncio_task(
        target,
        qsize=2**8,
        provide_channels=True,
        **kwargs,
    )
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

    # It looks like your event loop has caught a case of the ``trio``s.

    # :()

    # Don't worry, we've heard you'll barely notice. You might
    # hallucinate a few more propagating errors and feel like your
    # digestion has slowed but if anything get's too bad your parents
    # will know about it.

    # :)

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

        # TODO: shoudn't this be done in the guest-run trio task?
        # if debug_mode():
        #     # XXX make it obvi we know this isn't supported yet!
        #     log.error(
        #         'Attempting to enter unsupported `greenback` init '
        #         'from `asyncio` task..'
        #     )
        #     await _debug.maybe_init_greenback(
        #         force_reload=True,
        #     )

        def trio_done_callback(main_outcome):
            log.info(
                f'trio_main finished with\n'
                f'|_{main_outcome!r}'
            )

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

        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )
        fute_err: BaseException|None = None
        try:
            out: Outcome = await asyncio.shield(trio_done_fute)

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

        ) as fute_err:
            err_message: str = (
                'main `asyncio` task '
            )
            if isinstance(fute_err, asyncio.CancelledError):
                err_message += 'was cancelled!\n'
            else:
                err_message += f'errored with {out.error!r}\n'

            actor: tractor.Actor = tractor.current_actor()
            log.exception(
                err_message
                +
                'Cancelling `trio`-side `tractor`-runtime..\n'
                f'c)>\n'
                f'  |_{actor}.cancel_soon()\n'
            )

            # XXX WARNING XXX the next LOCs are super important, since
            # without them, we can get guest-run abandonment cases
            # where `asyncio` will not schedule or wait on the `trio`
            # guest-run task before final shutdown! This is
            # particularly true if the `trio` side has tasks doing
            # shielded work when a SIGINT condition occurs.
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
            except asyncio.exceptions.InvalidStateError as state_err:

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
