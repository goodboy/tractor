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
from tractor.runtime import _state
from tractor import log as logmod
from tractor.devx import (
    debug,
)

log = logmod.get_logger()


if TYPE_CHECKING:
    from tractor.spawn._spawn import ProcessType
    from tractor import (
        Actor,
        ActorNursery,
    )


@trio.lowlevel.disable_ki_protection
def dump_task_tree() -> None:
    '''
    Do a classic `stackscope.extract()` task-tree dump to console at
    `.devx()` level.

    Also unconditionally tee the rendered tree to two
    capture-bypassing sinks so SIGUSR1 dumps remain visible
    when the parent process has captured stdio (e.g. pytest's
    default `--capture=fd`):

    - `/tmp/tractor-stackscope-<pid>.log` (append-mode, always
      written) — guaranteed-readable artifact even under CI
      / `nohup` / no-tty conditions. `tail -f` to follow.
    - `/dev/tty` if a controlling terminal is attached —
      best-effort, ignored if the device is missing or write
      fails. pytest never captures the tty.

    '''
    import os
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

    fpath: str = f'/tmp/tractor-stackscope-{os.getpid()}.log'
    from . import _pformat
    actor_repr: str = _pformat.nest_from_op(
        input_op='|_',
        text=f'{actor}',
        nest_prefilx='|_',
        nest_indent=3,
    )
    full_dump: str = (
        f'Dumping `stackscope` tree for actor\n'
        f'(>: {actor.uid!r}\n'
        f' |_{mp.current_process()}\n'
        f'   |_{thr}\n'
        # TODO, use the nest_from_op
        f'{actor_repr}'
        # f'     |_{actor}'
        f'\n'
        f'{sigint_handler_report}\n'
        f'signal.getsignal(SIGINT) -> {current_sigint_handler!r}\n'
        f'\n'
        f'capture-bypass tee: {fpath}\n'
        f'(`tail -f {fpath}` to follow across signals)\n'
        f'\n'
        f'------ start-of-{actor.uid!r} ------\n'
        f'|\n'
        f'{tree_str}'
        f'|\n'
        f'|_____ end-of-{actor.uid!r} ______\n'
    )
    log.devx(full_dump)

    # NOTE, capture-bypass sinks. Pytest's default
    # `--capture=fd` swallows `log.devx()` above; the
    # following two writes guarantee the dump reaches the
    # human even when stdio is captured.
    try:
        with open(fpath, 'a') as f:
            f.write(full_dump + '\n')
    except OSError:
        log.exception(
            f'Failed to tee stackscope dump to {fpath!r}'
        )

    try:
        with open('/dev/tty', 'w') as tty:
            tty.write(full_dump + '\n')
    except OSError:
        # no controlling tty (CI / nohup / detached) —
        # silently fall through; the file sink covers it.
        pass

_handler_lock = RLock()
_tree_dumped: bool = False

# Captured at `enable_stack_on_sig()` time when running
# inside a trio task. `dump_tree_on_sig` uses this to
# schedule `dump_task_tree` ON the trio loop via
# `token.run_sync_soon` so stackscope sees a real current
# task and can recurse into nursery children. Without
# it (signal handler running in a non-trio stack frame),
# `stackscope.extract` only walks the `<init>` task and
# misses everything inside `async_main`'s nurseries.
_trio_token: trio.lowlevel.TrioToken|None = None


def _safe_dump_task_tree() -> None:
    '''
    `run_sync_soon`-friendly wrapper that swallows any
    exception from `dump_task_tree`. Trio prints
    + crashes on uncaught exceptions in scheduled
    callbacks; we'd rather log + keep the test running so
    the user can re-trigger the dump.

    '''
    try:
        dump_task_tree()
    except BaseException:
        log.exception(
            '`dump_task_tree()` raised (scheduled via '
            '`run_sync_soon`); continuing.\n'
        )


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
            # Prefer scheduling on the trio loop — runs the
            # dump from a real trio-task context so
            # `stackscope.extract(recurse_child_tasks=True)`
            # walks every nursery child instead of seeing
            # only the `<init>` task. Falls back to a direct
            # call when no token was captured (e.g. signal
            # delivered outside a trio.run).
            if _trio_token is not None:
                _trio_token.run_sync_soon(_safe_dump_task_tree)
            else:
                dump_task_tree()

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
        # NOTE, `stackscope._glue` does intentional async-gen type
        # introspection at import-time which trips
        # `RuntimeWarning: coroutine method 'asend'/'athrow' was
        # never awaited`. Benign — they only want the wrapper
        # type — but visible to users. Squelch the import-only
        # warning so SIGUSR1 setup stays quiet.
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore',
                category=RuntimeWarning,
                message=r"coroutine method '(asend|athrow)' .* was never awaited",
            )
            import stackscope
    except ImportError:
        log.warning(
            'The `stackscope` lib is not installed!\n'
            '`Ignoring enable_stack_on_sig() call!\n'
        )
        return None

    # Capture the trio token if we're inside `trio.run`
    # so SIGUSR1 dispatches the dump *onto* the trio loop
    # (full task-tree visibility). When called outside trio
    # (e.g. from `pytest_configure`), token capture fails
    # silently and `dump_tree_on_sig` falls back to the
    # direct-call path.
    global _trio_token
    try:
        _trio_token = trio.lowlevel.current_trio_token()
    except RuntimeError:
        # not in a `trio.run` — leave None; runtime can
        # re-call `enable_stack_on_sig()` later from
        # inside `async_main` to capture it.
        _trio_token = None

    handler: Callable|int = getsignal(sig)
    if handler is dump_tree_on_sig:
        log.devx(
            'A `SIGUSR1` handler already exists?\n'
            f'|_ {handler!r}\n'
            f'(trio_token captured: {_trio_token is not None})\n'
        )
        return

    signal(
        sig,
        dump_tree_on_sig,
    )
    log.devx(
        f'Enabling trace-trees on `SIGUSR1` '
        f'since `stackscope` is installed @ \n'
        f'{stackscope!r}\n\n'
        f'With `SIGUSR1` handler\n'
        f'|_{dump_tree_on_sig}\n'
        f'(trio_token captured: {_trio_token is not None})\n'
    )
    return stackscope
