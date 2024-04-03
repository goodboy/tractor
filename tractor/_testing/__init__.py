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
Various helpers/utils for auditing your `tractor` app and/or the
core runtime.

'''
from contextlib import asynccontextmanager as acm
import pathlib

import tractor
from .pytest import (
    tractor_test as tractor_test
)
from .fault_simulation import (
    break_ipc as break_ipc,
)


def repodir() -> pathlib.Path:
    '''
    Return the abspath to the repo directory.

    '''
    # 2 parents up to step up through tests/<repo_dir>
    return pathlib.Path(
        __file__

    # 3 .parents bc:
    # <._testing-pkg>.<tractor-pkg>.<git-repo-dir>
    # /$HOME/../<tractor-repo-dir>/tractor/_testing/__init__.py
    ).parent.parent.parent.absolute()


def examples_dir() -> pathlib.Path:
    '''
    Return the abspath to the examples directory as `pathlib.Path`.

    '''
    return repodir() / 'examples'


@acm
async def expect_ctxc(
    yay: bool,
    reraise: bool = False,
) -> None:
    '''
    Small acm to catch `ContextCancelled` errors when expected
    below it in a `async with ()` block.

    '''
    if yay:
        try:
            yield
            raise RuntimeError('Never raised ctxc?')
        except tractor.ContextCancelled:
            if reraise:
                raise
            else:
                return
    else:
        yield
