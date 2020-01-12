"""
Process spawning.

Mostly just wrapping around ``multiprocessing``.
"""
import multiprocessing as mp

# from . import log

import trio
import trio_run_in_process

try:
    from multiprocessing import semaphore_tracker  # type: ignore
    resource_tracker = semaphore_tracker
    resource_tracker._resource_tracker = resource_tracker._semaphore_tracker
except ImportError:
    # 3.8 introduces a more general version that also tracks shared mems
    from multiprocessing import resource_tracker  # type: ignore

from multiprocessing import forkserver  # type: ignore
from typing import Tuple

from . import _forkserver_override
from ._state import current_actor
from ._actor import Actor


_ctx: mp.context.BaseContext = mp.get_context("spawn")  # type: ignore


def try_set_start_method(name: str) -> mp.context.BaseContext:
    """Attempt to set the start method for ``multiprocess.Process`` spawning.

    If the desired method is not supported the sub-interpreter (aka "spawn"
    method) is used.
    """
    global _ctx

    allowed = mp.get_all_start_methods()

    if name not in allowed:
        name = 'spawn'
    elif name == 'fork':
        raise ValueError(
            "`fork` is unsupported due to incompatibility with `trio`"
        )
    elif name == 'forkserver':
        _forkserver_override.override_stdlib()

    assert name in allowed

    _ctx = mp.get_context(name)
    return _ctx


def is_main_process() -> bool:
    """Bool determining if this actor is running in the top-most process.
    """
    return mp.current_process().name == 'MainProcess'


async def new_proc(
    name: str,
    actor: Actor,
    # passed through to actor main
    bind_addr: Tuple[str, int],
    parent_addr: Tuple[str, int],
    nursery: trio.Nursery = None,
    use_trip: bool = True,
) -> mp.Process:
    """Create a new ``multiprocessing.Process`` using the
    spawn method as configured using ``try_set_start_method()``.
    """
    if use_trip:  # trio_run_in_process
        mng = trio_run_in_process.open_in_process(
            actor._trip_main,
            bind_addr,
            parent_addr,
            nursery=nursery,
        )
        # XXX playing with trip logging
        # l  = log.get_console_log(level='debug', name=None, _root_name='trio-run-in-process')
        # import logging
        # logger = logging.getLogger("trio-run-in-process")
        # logger.setLevel('DEBUG')
        proc = await mng.__aenter__()
        proc.mng = mng
        return proc
    else:
        # use multiprocessing
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
                    getattr(resource_tracker._resource_tracker, '_pid', None),
                    resource_tracker._resource_tracker._fd,
                )
            else:
                assert curr_actor._forkserver_info
                fs_info = (
                    fs._forkserver_address,
                    fs._forkserver_alive_fd,
                    fs._forkserver_pid,
                    resource_tracker._resource_tracker._pid,
                    resource_tracker._resource_tracker._fd,
                 ) = curr_actor._forkserver_info
        else:
            fs_info = (None, None, None, None, None)

        return _ctx.Process(
            target=actor._mp_main,
            args=(
                bind_addr,
                fs_info,
                start_method,
                parent_addr
            ),
            # daemon=True,
            name=name,
        )
