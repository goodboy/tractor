"""
Process spawning.

Mostly just wrapping around ``multiprocessing``.
"""
import multiprocessing as mp
from multiprocessing import forkserver, semaphore_tracker  # type: ignore
from typing import Tuple, Optional

from . import _forkserver_hackzorz
from ._state import current_actor
from ._actor import Actor


_ctx: mp.context.BaseContext = mp.get_context("spawn")


def try_set_start_method(name: str) -> mp.context.BaseContext:
    """Attempt to set the start method for ``multiprocess.Process`` spawning.

    If the desired method is not supported the sub-interpreter (aka "spawn"
    method) is used.
    """
    global _ctx

    allowed = mp.get_all_start_methods()
    if name not in allowed:
        name == 'spawn'

    assert name in allowed

    if name == 'forkserver':
        _forkserver_hackzorz.override_stdlib()

    _ctx = mp.get_context(name)
    return _ctx


def is_main_process() -> bool:
    """Bool determining if this actor is running in the top-most process.
    """
    return mp.current_process().name == 'MainProcess'


def new_proc(
    name: str,
    actor: Actor,
    # passed through to actor main
    bind_addr: Tuple[str, int],
    parent_addr: Tuple[str, int],
) -> mp.Process:
    """Create a new ``multiprocessing.Process`` using the
    spawn method as configured using ``try_set_start_method()``.
    """
    start_method = _ctx.get_start_method()
    if start_method == 'forkserver':
        # XXX do our hackery on the stdlib to avoid multiple
        # forkservers (one at each subproc layer).
        fs = forkserver._forkserver
        curr_actor = current_actor()
        if is_main_process() and not curr_actor._forkserver_info:
            # if we're the "main" process start the forkserver only once
            # and pass its ipc info to downstream children
            # forkserver.set_forkserver_preload(rpc_module_paths)
            forkserver.ensure_running()
            fs_info = (
                fs._forkserver_address,
                fs._forkserver_alive_fd,
                getattr(fs, '_forkserver_pid', None),
                getattr(semaphore_tracker._semaphore_tracker, '_pid', None),
                semaphore_tracker._semaphore_tracker._fd,
            )
        else:
            assert curr_actor._forkserver_info
            fs_info = (
                fs._forkserver_address,
                fs._forkserver_alive_fd,
                fs._forkserver_pid,
                semaphore_tracker._semaphore_tracker._pid,
                semaphore_tracker._semaphore_tracker._fd,
             ) = curr_actor._forkserver_info
    else:
        fs_info = (None, None, None, None, None)

    return _ctx.Process(
        target=actor._fork_main,
        args=(
            bind_addr,
            fs_info,
            start_method,
            parent_addr
        ),
        # daemon=True,
        name=name,
    )
