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
Erlang-style (ish) "one-cancels-one" nursery.

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
    nullcontext,
)
from typing import ContextManager

from outcome import (
    Outcome,
    acapture,
)
import pdbp
from msgspec import Struct
import trio
from trio._core._run import (
    Task,
    CancelScope,
    Nursery,
)

class MaybeOutcome(Struct):

    _ready: Event = trio.Event()
    _outcome: Outcome | None = None
    _result: Any | None = None

    @property
    def result(self) -> Any:
        '''
        Either Any or None depending on whether the Outcome has compeleted.

        '''
        if self._outcome is None:
            raise RuntimeError(
                # f'Task {task.name} is not complete.\n'
                f'Outcome is not complete.\n'
                'wait on `await MaybeOutcome.unwrap()` first!'
            )
        return self._result

    def _set_outcome(
        self,
        outcome: Outcome,
    ):
        self._outcome = outcome
        self._result = outcome.unwrap()
        self._ready.set()

    # TODO: maybe a better name like,
    # - .wait_and_unwrap()
    # - .wait_unwrap()
    # - .aunwrap() ?
    async def unwrap(self) -> Any:
        if self._ready.is_set():
            return self._result

        await self._ready.wait()

        out = self._outcome
        if out is None:
            raise ValueError(f'{out} is not an outcome!?')

        return self.result


class TaskHandle(Struct):
    task: Task
    cs: CancelScope
    exited: Event | None = None
    _outcome: Outcome | None = None


class ScopePerTaskNursery(Struct):
    _n: Nursery
    _scopes: dict[
        Task,
        tuple[CancelScope, Outcome]
    ] = {}

    scope_manager: ContextManager | None = None

    async def start_soon(
        self,
        async_fn,
        *args,

        name=None,
        scope_manager: ContextManager | None = None,

    ) -> tuple[CancelScope, Task]:

        # NOTE: internals of a nursery don't let you know what
        # the most recently spawned task is by order.. so we'd
        # have to either change that or do set ops.
        # pre_start_tasks: set[Task] = n._children.copy()
        # new_tasks = n._children - pre_start_Tasks
        # assert len(new_tasks) == 1
        # task = new_tasks.pop()

        n: Nursery = self._n
        cs = CancelScope()
        new_task: Task | None = None
        to_return: tuple[Any] | None = None
        maybe_outcome = MaybeOutcome()

        sm = self.scope_manager
        if sm is None:
            mngr = nullcontext([cs])
        else:
            mngr = sm(
                nursery=n,
                scope=cs,
                maybe_outcome=maybe_outcome,
            )

        async def _start_wrapped_in_scope(
            task_status: TaskStatus[
                tuple[CancelScope, Task]
            ] = trio.TASK_STATUS_IGNORED,

        ) -> None:
            nonlocal maybe_outcome
            nonlocal to_return

            with cs:

                task = trio.lowlevel.current_task()
                self._scopes[cs] = task

                # TODO: instead we should probably just use
                # `Outcome.send(mngr)` here no and inside a custom
                # decorator `@trio.cancel_scope_manager` enforce
                # that it's a single yield generator?
                with mngr as to_return:

                    # TODO: relay through whatever the
                    # started task passes back via `.started()` ?
                    # seems like that won't work with also returning
                    # a "task handle"?
                    task_status.started()

                    # invoke underlying func now that cs is entered.
                    outcome = await acapture(async_fn, *args)

                    # TODO: instead, mngr.send(outcome) so that we don't
                    # tie this `.start_soon()` impl to the
                    # `MaybeOutcome` type? Use `Outcome.send(mngr)`
                    # right?
                    maybe_outcome._set_outcome(outcome)

        await n.start(_start_wrapped_in_scope)
        assert to_return is not None

        # TODO: better way to concat the values delivered by the user
        # provided `.scope_manager` and the outcome?
        return tuple([maybe_outcome] + to_return)


# TODO: maybe just make this a generator with a single yield that also
# delivers a value (of some std type) from the yield expression?
# @trio.cancel_scope_manager
@cm
def add_task_handle_and_crash_handling(
    nursery: Nursery,
    scope: CancelScope,
    maybe_outcome: MaybeOutcome,

) -> Generator[None, list[Any]]:

    cs: CancelScope = CancelScope()

    # if you need it you can ask trio for the task obj
    task: Task = trio.lowlevel.current_task()
    print(f'Spawning task: {task.name}')

    try:
        # yields back when task is terminated, cancelled, returns?
        with cs:
            # the yielded values here are what are returned to the
            # nursery's `.start_soon()` caller

            # TODO: actually make this work so that `MaybeOutcome` isn't
            # tied to the impl of `.start_soon()` on our custom nursery!
            task_outcome: Outcome = yield [cs]

    except Exception as err:
        # Adds "crash handling" from `pdbp` by entering
        # a REPL on std errors.
        pdbp.xpm()
        raise


@acm
async def open_nursery(
    scope_manager = None,
    **kwargs,
):
    async with trio.open_nursery(**kwargs) as nurse:
        yield ScopePerTaskNursery(
            nurse,
            scope_manager=scope_manager,
        )


async def sleep_then_err():
    await trio.sleep(1)
    assert 0


async def sleep_then_return_val(val: str):
    await trio.sleep(0.2)
    return val


if __name__ == '__main__':

    async def main():
        async with open_nursery(
            scope_manager=add_task_handle_and_crash_handling,
        ) as sn:
            for _ in range(3):
                outcome, cs = await sn.start_soon(trio.sleep_forever)

            # extra task we want to engage in debugger post mortem.
            err_outcome, *_ = await sn.start_soon(sleep_then_err)

            val: str = 'yoyoyo'
            val_outcome, cs = await sn.start_soon(sleep_then_return_val, val)
            res = await val_outcome.unwrap()
            assert res == val
            print(f'GOT EXPECTED TASK VALUE: {res}')

            print('WAITING FOR CRASH..')

    trio.run(main)
