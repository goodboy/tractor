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
# from functools import partial
from threading import (
    current_thread,
    Thread,
    RLock,
)
import multiprocessing as mp
from signal import (
    signal,
    getsignal,
    SIGUSR1,
    SIGINT,
)
# import traceback
from types import ModuleType
from typing import (
    Callable,
    TYPE_CHECKING,
)

import trio
from tractor import (
    _state,
    log as logmod,
)
from tractor.devx import debug

log = logmod.get_logger(__name__)


if TYPE_CHECKING:
    from tractor._spawn import ProcessType
    from tractor import (
        Actor,
        ActorNursery,
    )


@trio.lowlevel.disable_ki_protection
def dump_task_tree() -> None:
    '''
    Do a classic `stackscope.extract()` task-tree dump to console at
    `.devx()` level.

    '''
    import stackscope
    tree_str: str = str(
        stackscope.extract(
            trio.lowlevel.current_root_task(),
            recurse_child_tasks=True
        )
    )
    actor: Actor = _state.current_actor()
    thr: Thread = current_thread()
    current_sigint_handler: Callable = getsignal(SIGINT)
    if (
        current_sigint_handler
        is not
        debug.DebugStatus._trio_handler
    ):
        sigint_handler_report: str = (
            'The default `trio` SIGINT handler was replaced?!'
        )
    else:
        sigint_handler_report: str = (
            'The default `trio` SIGINT handler is in use?!'
        )

    # sclang symbology
    # |_<object>
    # |_(Task/Thread/Process/Actor
    # |_{Supervisor/Scope
    # |_[Storage/Memory/IPC-Stream/Data-Struct

    log.devx(
        f'Dumping `stackscope` tree for actor\n'
        f'(>: {actor.uid!r}\n'
        f' |_{mp.current_process()}\n'
        f'   |_{thr}\n'
        f'     |_{actor}\n'
        f'\n'
        f'{sigint_handler_report}\n'
        f'signal.getsignal(SIGINT) -> {current_sigint_handler!r}\n'
        # f'\n'
        # start-of-trace-tree delimiter (mostly for testing)
        # f'------ {actor.uid!r} ------\n'
        f'\n'
        f'------ start-of-{actor.uid!r} ------\n'
        f'|\n'
        f'{tree_str}'
        # end-of-trace-tree delimiter (mostly for testing)
        f'|\n'
        f'|_____ end-of-{actor.uid!r} ______\n'
    )
    # TODO: can remove this right?
    # -[ ] was original code from author
    #
    # print(
    #     'DUMPING FROM PRINT\n'
    #     +
    #     content
    # )
    # import logging
    # try:
    #     with open("/dev/tty", "w") as tty:
    #         tty.write(tree_str)
    # except BaseException:
    #     logging.getLogger(
    #         "task_tree"
    #     ).exception("Error printing task tree")

_handler_lock = RLock()
_tree_dumped: bool = False


def dump_tree_on_sig(
    sig: int,
    frame: object,

    relay_to_subs: bool = True,

) -> None:
    global _tree_dumped, _handler_lock
    with _handler_lock:
        # if _tree_dumped:
        #     log.warning(
        #         'Already dumped for this actor...??'
        #     )
        #     return

        _tree_dumped = True

        # actor: Actor = _state.current_actor()
        log.devx(
            'Trying to dump `stackscope` tree..\n'
        )
        try:
            dump_task_tree()
            # await actor._service_n.start_soon(
            #     partial(
            #         trio.to_thread.run_sync,
            #         dump_task_tree,
            #     )
            # )
            # trio.lowlevel.current_trio_token().run_sync_soon(
            #     dump_task_tree
            # )

        except RuntimeError:
            log.exception(
                'Failed to dump `stackscope` tree..\n'
            )
            # not in async context -- print a normal traceback
            # traceback.print_stack()
            raise

        except BaseException:
            log.exception(
                'Failed to dump `stackscope` tree..\n'
            )
            raise

        # log.devx(
        #     'Supposedly we dumped just fine..?'
        # )

    if not relay_to_subs:
        return

    an: ActorNursery
    for an in _state.current_actor()._actoruid2nursery.values():
        subproc: ProcessType
        subactor: Actor
        for subactor, subproc, _ in an._children.values():
            log.warning(
                f'Relaying `SIGUSR1`[{sig}] to sub-actor\n'
                f'{subactor}\n'
                f' |_{subproc}\n'
            )

            # bc of course stdlib can't have a std API.. XD
            match subproc:
                case trio.Process():
                    subproc.send_signal(sig)

                case mp.Process():
                    subproc._send_signal(sig)


def enable_stack_on_sig(
    sig: int = SIGUSR1,
) -> ModuleType:
    '''
    Enable `stackscope` tracing on reception of a signal; by
    default this is SIGUSR1.

    HOT TIP: a task/ctx-tree dump can be triggered from a shell with
    fancy cmds.

    For ex. from `bash` using `pgrep` and cmd-sustitution
    (https://www.gnu.org/software/bash/manual/bash.html#Command-Substitution)
    you could use:

    >> kill -SIGUSR1 $(pgrep -f <part-of-cmd: str>)

    OR without a sub-shell,

    >> pkill --signal SIGUSR1 -f <part-of-cmd: str>

    '''
    try:
        import stackscope
    except ImportError:
        log.warning(
            '`stackscope` not installed for use in debug mode!'
        )
        return None

    handler: Callable|int = getsignal(sig)
    if handler is dump_tree_on_sig:
        log.devx(
            'A `SIGUSR1` handler already exists?\n'
            f'|_ {handler!r}\n'
        )
        return

    signal(
        sig,
        dump_tree_on_sig,
    )
    log.devx(
        'Enabling trace-trees on `SIGUSR1` '
        'since `stackscope` is installed @ \n'
        f'{stackscope!r}\n\n'
        f'With `SIGUSR1` handler\n'
        f'|_{dump_tree_on_sig}\n'
    )
    return stackscope
