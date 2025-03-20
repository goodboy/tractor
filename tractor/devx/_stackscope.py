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
from signal import (
    signal,
    SIGUSR1,
)

import trio

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
    log = get_console_log('cancel')
    log.pdb(
        f'Dumping `stackscope` tree:\n\n'
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


def signal_handler(sig: int, frame: object) -> None:
    import traceback
    try:
        trio.lowlevel.current_trio_token(
        ).run_sync_soon(dump_task_tree)
    except RuntimeError:
        # not in async context -- print a normal traceback
        traceback.print_stack()



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
