"""
Process spawning.

Mostly just wrapping around ``multiprocessing``.
"""
import multiprocessing as mp
from multiprocessing import forkserver, semaphore_tracker  # type: ignore
from typing import Tuple

from . import _forkserver_hackzorz
from ._state import current_actor
from ._actor import Actor


_forkserver_hackzorz.override_stdlib()
ctx = mp.get_context("forkserver")


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

    return ctx.Process(
        target=actor._fork_main,
        args=(bind_addr, fs_info, parent_addr),
        # daemon=True,
        name=name,
    )
