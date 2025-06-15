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
`trio.Task` cancellation helpers, extensions and "holsters".

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
)

import trio
# from trio import TaskStatus


@acm
async def maybe_raise_from_masking_exc(
    tn: trio.Nursery,
    unmask_from: BaseException|None = trio.Cancelled

    # TODO, maybe offer a collection?
    # unmask_from: set[BaseException] = {
    #     trio.Cancelled,
    # },
):
    if not unmask_from:
        yield
        return

    try:
        yield
    except* unmask_from as be_eg:

        # TODO, if we offer `unmask_from: set`
        # for masker_exc_type in unmask_from:

        matches, rest = be_eg.split(unmask_from)
        if not matches:
            raise

        for exc_match in be_eg.exceptions:
            if (
                (exc_ctx := exc_match.__context__)
                and
                type(exc_ctx) not in {
                    # trio.Cancelled,  # always by default?
                    unmask_from,
                }
            ):
                exc_ctx.add_note(
                    f'\n'
                    f'WARNING: the above error was masked by a {unmask_from!r} !?!\n'
                    f'Are you always cancelling? Say from a `finally:` ?\n\n'

                    f'{tn!r}'
                )
                raise exc_ctx from exc_match
