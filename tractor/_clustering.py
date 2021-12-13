# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

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
