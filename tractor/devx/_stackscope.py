# tractor: structured concurrent "actors".
# Copyright eternity Tyler Goodlet.

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
The fundamental cross process SC abstraction: an inter-actor,
cancel-scope linked task "context".

A ``Context`` is very similar to the ``trio.Nursery.cancel_scope`` built
into each ``trio.Nursery`` except it links the lifetimes of memory space
disjoint, parallel executing tasks in separate actors.

'''
from __future__ import annotations
import multiprocessing as mp
from signal import (
    signal,
    SIGUSR1,
)
import traceback
from typing import TYPE_CHECKING

import trio
from tractor import (
    _state,
    log as logmod,
)

log = logmod.get_logger(__name__)


if TYPE_CHECKING:
    from tractor._spawn import ProcessType
    from tractor import (
        Actor,
        ActorNursery,
    )


@trio.lowlevel.disable_ki_protection
def dump_task_tree() -> None:
    import stackscope
    from tractor.log import get_console_log

    tree_str: str = str(
        stackscope.extract(
            trio.lowlevel.current_root_task(),
            recurse_child_tasks=True
        )
    )
    log = get_console_log(
        name=__name__,
        level='cancel',
    )
    actor: Actor = _state.current_actor()
    log.pdb(
        f'Dumping `stackscope` tree for actor\n'
        f'{actor.name}: {actor}\n'
        f' |_{mp.current_process()}\n\n'
        f'{tree_str}\n'
    )
    # import logging
    # try:
    #     with open("/dev/tty", "w") as tty:
    #         tty.write(tree_str)
    # except BaseException:
    #     logging.getLogger(
    #         "task_tree"
    #     ).exception("Error printing task tree")


def signal_handler(
    sig: int,
    frame: object,

    relay_to_subs: bool = True,

) -> None:
    try:
        trio.lowlevel.current_trio_token(
        ).run_sync_soon(dump_task_tree)
    except RuntimeError:
        # not in async context -- print a normal traceback
        traceback.print_stack()

    if not relay_to_subs:
        return

    an: ActorNursery
    for an in _state.current_actor()._actoruid2nursery.values():

        subproc: ProcessType
        subactor: Actor
        for subactor, subproc, _ in an._children.values():
            log.pdb(
                f'Relaying `SIGUSR1`[{sig}] to sub-actor\n'
                f'{subactor}\n'
                f' |_{subproc}\n'
            )

            if isinstance(subproc, trio.Process):
                subproc.send_signal(sig)

            elif isinstance(subproc, mp.Process):
                subproc._send_signal(sig)


def enable_stack_on_sig(
    sig: int = SIGUSR1
) -> None:
    '''
    Enable `stackscope` tracing on reception of a signal; by
    default this is SIGUSR1.

    '''
    signal(
        sig,
        signal_handler,
    )
    # NOTE: not the above can be triggered from
    # a (xonsh) shell using:
    # kill -SIGUSR1 @$(pgrep -f '<cmd>')
    #
    # for example if you were looking to trace a `pytest` run
    # kill -SIGUSR1 @$(pgrep -f 'pytest')
