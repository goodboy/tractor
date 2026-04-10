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
Erlang-style (ish) "one-cancels-one" nursery, what we just call
a "task manager".

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
    # contextmanager as cm,
)
from functools import partial
from typing import (
    Generator,
    Any,
)

from outcome import (
    Outcome,
    acapture,
)
from msgspec import Struct
import trio
from trio import (
    TaskStatus,
    CancelScope,
    Nursery,
)
from trio.lowlevel import (
    Task,
)
from tractor.log import get_logger

log = get_logger(__name__)


class TaskOutcome(Struct):
    '''
    The outcome of a scheduled ``trio`` task which includes an interface
    for synchronizing to the completion of the task's runtime and access
    to the eventual boxed result/value or raised exception.

    '''
    lowlevel_task: Task
    _exited = trio.Event()  # as per `trio.Runner.task_exited()`
    _outcome: Outcome | None = None  # as per `outcome.Outcome`
    _result: Any | None = None  # the eventual maybe-returned-value

    @property
    def result(self) -> Any:
        '''
        Either Any or None depending on whether the Outcome has compeleted.

        '''
        if self._outcome is None:
            raise RuntimeError(
                f'Task {self.lowlevel_task.name} is not complete.\n'
                'First wait on `await TaskOutcome.wait_for_result()`!'
            )
        return self._result

    def _set_outcome(
        self,
        outcome: Outcome,
    ):
        '''
        Set the ``Outcome`` for this task.

        This method should only ever be called by the task's supervising
        nursery implemenation.

        '''
        self._outcome = outcome
        self._result = outcome.unwrap()
        self._exited.set()

    async def wait_for_result(self) -> Any:
        '''
        Unwind the underlying task's ``Outcome`` by async waiting for
        the task to first complete and then unwrap it's result-value.

        '''
        if self._exited.is_set():
            return self._result

        await self._exited.wait()

        out = self._outcome
        if out is None:
            raise ValueError(f'{out} is not an outcome!?')

        return self.result


class TaskManagerNursery(Struct):
    _tn: Nursery
    _scopes: dict[
        Task,
        tuple[CancelScope, Outcome]
    ] = {}

    task_manager: Generator[Any, Outcome, None] | None = None

    async def start_soon(
        self,
        async_fn,
        *args,

        name=None,
        task_manager: Generator[Any, Outcome, None] | None = None

    ) -> tuple[CancelScope, Task]:

        # NOTE: internals of a nursery don't let you know what
        # the most recently spawned task is by order.. so we'd
        # have to either change that or do set ops.
        # pre_start_tasks: set[Task] = n._children.copy()
        # new_tasks = n._children - pre_start_Tasks
        # assert len(new_tasks) == 1
        # task = new_tasks.pop()

        tn: Nursery = self._tn

        sm = self.task_manager
        # we do default behavior of a scope-per-nursery
        # if the user did not provide a task manager.
        if sm is None:
            return tn.start_soon(async_fn, *args, name=None)

        # new_task: Task|None = None
        to_return: tuple[Any] | None = None

        # NOTE: what do we enforce as a signature for the
        # `@task_scope_manager` here?
        mngr = sm(nursery=tn)

        async def _start_wrapped_in_scope(
            task_status: TaskStatus[
                tuple[CancelScope, Task]
            ] = trio.TASK_STATUS_IGNORED,

        ) -> None:

            # TODO: this was working before?! and, do we need something
            # like it to implement `.start()`?
            # nonlocal to_return

            # execute up to the first yield
            try:
                to_return: tuple[Any] = next(mngr)
            except StopIteration:
                raise RuntimeError("task manager didn't yield") from None

            # TODO: how do we support `.start()` style?
            # - relay through whatever the
            #   started task passes back via `.started()` ?
            #   seems like that won't work with also returning
            #   a "task handle"?
            # - we were previously binding-out this `to_return` to
            #   the parent's lexical scope, why isn't that working
            #   now?
            task_status.started(to_return)

            # invoke underlying func now that cs is entered.
            outcome = await acapture(async_fn, *args)

            # execute from the 1st yield to return and expect
            # generator-mngr `@task_scope_manager` thinger to
            # terminate!
            try:
                mngr.send(outcome)


                # I would presume it's better to have a handle to
                # the `Outcome` entirely? This method sends *into*
                # the mngr this `Outcome.value`; seems like kinda
                # weird semantics for our purposes?
                # outcome.send(mngr)

            except StopIteration:
                return
            else:
                raise RuntimeError(f"{mngr} didn't stop!")

        to_return = await tn.start(_start_wrapped_in_scope)
        assert to_return is not None

        # TODO: use the fancy type-check-time type signature stuff from
        # mypy i guess..to like, relay the type of whatever the
        # generator yielded through? betcha that'll be un-grokable XD
        return to_return


# TODO: define a decorator to runtime type check that this a generator
# with a single yield that also delivers a value (of some std type) from
# the yield expression?
# @trio.task_manager
def add_task_handle_and_crash_handling(
    nursery: Nursery,

    debug_mode: bool = False,

) -> Generator[
    Any,
    Outcome,
    None,
]:
    '''
    A customizable, user defined "task scope manager".

    With this specially crafted single-yield generator function you can
    add more granular controls around every task spawned by `trio` B)

    '''
    # if you need it you can ask trio for the task obj
    task: Task = trio.lowlevel.current_task()
    log.info(f'Spawning task: {task.name}')

    # User defined "task handle" for more granular supervision
    # of each spawned task as needed for their particular usage.
    task_outcome = TaskOutcome(task)

    # NOTE: if wanted the user could wrap the output task handle however
    # they want!
    # class TaskHandle(Struct):
    #     task: Task
    #     cs: CancelScope
    #     outcome: TaskOutcome

    # this yields back when the task is terminated, cancelled or returns.
    try:
        with CancelScope() as cs:

            # the yielded value(s) here are what are returned to the
            # nursery's `.start_soon()` caller B)
            lowlevel_outcome: Outcome = yield (task_outcome, cs)
            task_outcome._set_outcome(lowlevel_outcome)

    # Adds "crash handling" from `pdbp` by entering
    # a REPL on std errors.
    except Exception as err:
        if debug_mode:
            log.exception(
                f'{task.name} crashed, entering debugger!'
            )
            import pdbp
            pdbp.xpm()

        raise err

    finally:
        log.info(
            f'Task exitted\n'
            f')>\n'
            f' |_{task}\n'
            # ^^TODO? use sclang formatter?
            # -[ ] .devx.pformat.nest_from_op()` yo!
        )


@acm
async def open_taskman(
    task_manager: Generator[Any, Outcome, None] | None = None,

    **lowlevel_nursery_kwargs,
):
    async with trio.open_nursery(**lowlevel_nursery_kwargs) as nurse:
        yield TaskManagerNursery(
            nurse,
            task_manager=task_manager,
        )


async def sleep_then_return_val(val: str):
    await trio.sleep(0.2)
    return val


async def ensure_cancelled():
    try:
        await trio.sleep_forever()

    except trio.Cancelled:
        task = trio.lowlevel.current_task()
        log.cancel(f'heyyo ONLY {task.name} was cancelled as expected B)')
        assert 0

    except BaseException:
        raise RuntimeError("woa woa woa this ain't right!")


if __name__ == '__main__':

    from tractor.log import get_console_log
    get_console_log(level='info')

    async def main():
        async with open_taskman(
            task_manager=partial(
                add_task_handle_and_crash_handling,
                debug_mode=True,
            ),
        ) as tm:
            for _ in range(3):
                outcome, _ = await tm.start_soon(trio.sleep_forever)

            # extra task we want to engage in debugger post mortem.
            err_outcome, cs = await tm.start_soon(ensure_cancelled)

            val: str = 'yoyoyo'
            val_outcome, _ = await tm.start_soon(
                sleep_then_return_val,
                val,
            )
            res = await val_outcome.wait_for_result()
            assert res == val
            log.info(f'{res} -> GOT EXPECTED TASK VALUE')

            await trio.sleep(0.6)
            log.cancel(
                f'Cancelling and waiting on {err_outcome.lowlevel_task} '
                'to CRASH..'
            )
            cs.cancel()

    trio.run(main)
