'''
Actor cluster helpers.

'''
from __future__ import annotations

from contextlib import asynccontextmanager as acm
from multiprocessing import cpu_count
from typing import AsyncGenerator, Optional

import trio
import tractor


@acm
async def open_actor_cluster(
    modules: list[str],
    count: int = cpu_count(),
    names: Optional[list[str]] = None,
    start_method: Optional[str] = None,
    hard_kill: bool = False,
) -> AsyncGenerator[
    dict[str, tractor.Portal],
    None,
]:

    portals: dict[str, tractor.Portal] = {}

    if not names:
        names = [f'worker_{i}' for i in range(count)]

    if not len(names) == count:
        raise ValueError(
            'Number of names is {len(names)} but count it {count}')

    async with tractor.open_nursery(start_method=start_method) as an:
        async with trio.open_nursery() as n:
            uid = tractor.current_actor().uid

            async def _start(name: str) -> None:
                name = f'{uid[0]}.{name}'
                portals[name] = await an.start_actor(
                    enable_modules=modules,
                    name=name,
                )

            for name in names:
                n.start_soon(_start, name)

        assert len(portals) == count
        yield portals

        await an.cancel(hard_kill=hard_kill)
