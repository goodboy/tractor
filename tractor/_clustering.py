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

) -> AsyncGenerator[
    list[str],
    dict[str, tractor.Portal]
]:

    portals: dict[str, tractor.Portal] = {}
    uid = __import__('random').randint(0, 2 ** 16)
    # uid = tractor.current_actor().uid

    if not names:
        suffix = '_'.join(uid)
        names = [f'worker_{i}.' + suffix for i in range(count)]

    if not len(names) == count:
        raise ValueError(
            'Number of names is {len(names)} but count it {count}')

    async with tractor.open_nursery() as an:
        async with trio.open_nursery() as n:
            for index, key in zip(range(count), names):

                async def start(i) -> None:
                    key = f'worker_{i}.' + '_'.join(uid)
                    portals[key] = await an.start_actor(
                        enable_modules=modules,
                        name=key,
                    )

                n.start_soon(start, index)

        assert len(portals) == count
        yield portals
