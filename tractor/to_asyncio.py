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
import asyncio
from asyncio.exceptions import CancelledError
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass
import inspect
from typing import (
    Any,
    Callable,
    AsyncIterator,
    Awaitable,
    Optional,
)

import trio
from outcome import Error

from .log import get_logger
from ._state import current_actor
from ._exceptions import AsyncioCancelled
from .trionics._broadcast import (
    broadcast_receiver,
    BroadcastReceiver,
)

log = get_logger(__name__)


__all__ = ['run_task', 'run_as_asyncio_guest']


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
    _aio_task: Optional[asyncio.Task] = None
    _aio_err: Optional[BaseException] = None
    _broadcaster: Optional[BroadcastReceiver] = None

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
    **kwargs,

) -> LinkedTaskChannel:
    '''
    Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.

    '''
    __tracebackhide__ = True
    if not current_actor().is_infected_aio():
        raise RuntimeError("`infect_asyncio` mode is not enabled!?")

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
    aio_err: Optional[BaseException] = None

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
            log.exception('asyncio task errored')
            chan._aio_err = aio_err
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

    task = asyncio.create_task(
        wait_on_coro_final_result(
            to_trio,
            coro,
            aio_task_complete
        )
    )
    chan._aio_task = task

    def cancel_trio(task: asyncio.Task) -> None:
        '''
        Cancel the calling ``trio`` task on error.

        '''
        nonlocal chan
        aio_err = chan._aio_err
        task_err: Optional[BaseException] = None

        # only to avoid ``asyncio`` complaining about uncaptured
        # task exceptions
        try:
            task.exception()
        except BaseException as terr:
            task_err = terr

            if isinstance(terr, CancelledError):
                log.cancel(f'`asyncio` task cancelled: {task.get_name()}')
            else:
                log.exception(f'`asyncio` task: {task.get_name()} errored')

            assert type(terr) is type(aio_err), 'Asyncio task error mismatch?'

        if aio_err is not None:
            # XXX: uhh is this true?
            # assert task_err, f'Asyncio task {task.get_name()} discrepancy!?'

            # NOTE: currently mem chan closure may act as a form
            # of error relay (at least in the ``asyncio.CancelledError``
            # case) since we have no way to directly trigger a ``trio``
            # task error without creating a nursery to throw one.
            # We might want to change this in the future though.
            from_aio.close()

            if type(aio_err) is CancelledError:
                log.cancel("infected task was cancelled")

                # TODO: show that the cancellation originated
                # from the ``trio`` side? right?
                # if cancel_scope.cancelled:
                #     raise aio_err from err

            elif task_err is None:
                assert aio_err
                aio_err.with_traceback(aio_err.__traceback__)
                log.error('infected task errorred')

            # XXX: alway cancel the scope on error
            # in case the trio task is blocking
            # on a checkpoint.
            cancel_scope.cancel()

            # raise any ``asyncio`` side error.
            raise aio_err

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

    aio_err: Optional[BaseException] = None

    # TODO: make thisi a channel method?
    def maybe_raise_aio_err(
        err: Optional[Exception] = None
    ) -> None:
        aio_err = chan._aio_err
        if (
            aio_err is not None and
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
            task.cancelled() and
            type(aio_err) is CancelledError
        ):
            # if an underlying ``asyncio.CancelledError`` triggered this
            # channel close, raise our (non-``BaseException``) wrapper
            # error: ``AsyncioCancelled`` from that source error.
            raise AsyncioCancelled from aio_err

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
    Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.

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
            first = await chan.receive()

            # deliver stream handle upward
            try:
                with chan._trio_cs:
                    yield first, chan
            finally:
                chan._trio_exited = True
                chan._to_trio.close()


def run_as_asyncio_guest(

    trio_main: Callable,

) -> None:
    '''
    Entry for an "infected ``asyncio`` actor".

    Entrypoint for a Python process which starts the ``asyncio`` event
    loop and runs ``trio`` in guest mode resulting in a system where
    ``trio`` tasks can control ``asyncio`` tasks whilst maintaining
    SC semantics.

    '''
    # Uh, oh. :o

    # It looks like your event loop has caught a case of the ``trio``s.

    # :()

    # Don't worry, we've heard you'll barely notice. You might hallucinate
    # a few more propagating errors and feel like your digestion has
    # slowed but if anything get's too bad your parents will know about
    # it.

    # :)

    async def aio_main(trio_main):

        loop = asyncio.get_running_loop()
        trio_done_fut = asyncio.Future()

        def trio_done_callback(main_outcome):

            if isinstance(main_outcome, Error):
                error = main_outcome.error
                trio_done_fut.set_exception(error)

                # TODO: explicit asyncio tb?
                # traceback.print_exception(error)

                # XXX: do we need this?
                # actor.cancel_soon()

                main_outcome.unwrap()
            else:
                trio_done_fut.set_result(main_outcome)
                log.runtime(f"trio_main finished: {main_outcome!r}")

        # start the infection: run trio on the asyncio loop in "guest mode"
        log.info(f"Infecting asyncio process with {trio_main}")

        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )
        # ``.unwrap()`` will raise here on error
        return (await trio_done_fut).unwrap()

    # might as well if it's installed.
    try:
        import uvloop
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)
    except ImportError:
        pass

    return asyncio.run(aio_main(trio_main))
