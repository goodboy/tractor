"""
Integration test: spawning tractor actors from an MPI process.

When a parent is launched via ``mpirun``, Open MPI sets ``OMPI_*`` env
vars that bind ``MPI_Init`` to the ``orted`` daemon.  Tractor children
inherit those env vars, so if ``inherit_parent_main=True`` (the default)
the child re-executes ``__main__``, re-imports ``mpi4py``, and
``MPI_Init_thread`` fails because the child was never spawned by
``orted``::

    getting local rank failed
      --> Returned value No permission (-17) instead of ORTE_SUCCESS

Passing ``inherit_parent_main=False`` and placing RPC functions in a
separate importable module (``_child``) avoids the re-import entirely.

Usage::

    mpirun --allow-run-as-root -np 1 python -m \
        examples.integration.mpi4py.inherit_parent_main
"""

from mpi4py import MPI

import os
import trio
import tractor

from ._child import child_fn


async def main() -> None:
    rank = MPI.COMM_WORLD.Get_rank()
    print(f"[parent] rank={rank}  pid={os.getpid()}", flush=True)

    async with tractor.open_nursery(start_method='trio') as an:
        portal = await an.start_actor(
            'mpi-child',
            enable_modules=[child_fn.__module__],
            # Without this the child replays __main__, which
            # re-imports mpi4py and crashes on MPI_Init.
            inherit_parent_main=False,
        )
        result = await portal.run(child_fn)
        print(f"[parent] got: {result}", flush=True)
        await portal.cancel_actor()


if __name__ == "__main__":
    trio.run(main)
