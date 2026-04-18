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
Hang-diagnostic helpers for concurrent / multi-interpreter code.

Collected from the `subint` spawn backend bringup (issue #379)
where silent test-suite hangs needed careful teardown
instrumentation to diagnose. This module bottles up the
techniques that actually worked so future hangs are faster
to corner.

Two primitives:

1. `dump_on_hang()` — context manager wrapping
   `faulthandler.dump_traceback_later()` with the critical
   gotcha baked in: write the dump to a **file**, not
   `sys.stderr`. Under `pytest` (and any other output
   capturer) stderr gets swallowed and the dump is easy to
   miss — burning hours convinced you're looking at the wrong
   thing.

2. `track_resource_deltas()` — context manager (+ optional
   autouse-fixture factory) logging per-block deltas of
   `threading.active_count()` and — if running on py3.13+ —
   `len(_interpreters.list_all())`. Lets you quickly rule out
   leak-accumulation theories when a suite hangs more
   frequently as it progresses (if counts don't grow, it's
   not a leak; look for a race on shared cleanup instead).

See issue #379 / commit `26fb820` for the worked example.

'''
from __future__ import annotations
import faulthandler
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Callable,
    Iterator,
)

try:
    import _interpreters  # type: ignore
except ImportError:
    _interpreters = None  # type: ignore


__all__ = [
    'dump_on_hang',
    'track_resource_deltas',
    'resource_delta_fixture',
]


@contextmanager
def dump_on_hang(
    seconds: float = 30.0,
    *,
    path: str | Path = '/tmp/tractor_hang.dump',
    all_threads: bool = True,

) -> Iterator[str]:
    '''
    Arm `faulthandler` to dump all-thread tracebacks to
    `path` after `seconds` if the with-block hasn't exited.

    *Writes to a file, not stderr* — `pytest`'s stderr
    capture silently eats stderr-destined `faulthandler`
    output, and the same happens under any framework that
    redirects file-descriptors. Pointing the dump at a real
    file sidesteps that.

    Yields the resolved file path so it's easy to read back.

    Example
    -------
    ::

        from tractor.devx import dump_on_hang

        def test_hang():
            with dump_on_hang(
                seconds=15,
                path='/tmp/my_test_hang.dump',
            ) as dump_path:
                trio.run(main)
            # if it hangs, inspect dump_path afterward

    '''
    dump_path = Path(path)
    f = dump_path.open('w')
    try:
        faulthandler.dump_traceback_later(
            seconds,
            repeat=False,
            file=f,
            exit=False,
        )
        try:
            yield str(dump_path)
        finally:
            faulthandler.cancel_dump_traceback_later()
    finally:
        f.close()


def _snapshot() -> tuple[int, int]:
    '''
    Return `(thread_count, subint_count)`.

    Subint count reported as `0` on pythons lacking the
    private `_interpreters` stdlib module (i.e. py<3.13).

    '''
    threads: int = threading.active_count()
    subints: int = (
        len(_interpreters.list_all())
        if _interpreters is not None
        else 0
    )
    return threads, subints


@contextmanager
def track_resource_deltas(
    label: str = '',
    *,
    writer: Callable[[str], None] | None = None,

) -> Iterator[tuple[int, int]]:
    '''
    Log `(threads, subints)` deltas across the with-block.

    `writer` defaults to `sys.stderr.write` (+ trailing
    newline); pass a custom callable to route elsewhere
    (e.g., a log handler or an append-to-file).

    Yields the pre-entry snapshot so callers can assert
    against the expected counts if they want.

    Example
    -------
    ::

        from tractor.devx import track_resource_deltas

        async def test_foo():
            with track_resource_deltas(label='test_foo'):
                async with tractor.open_nursery() as an:
                    ...

        # Output:
        #   test_foo: threads 2->2, subints 1->1

    '''
    before = _snapshot()
    try:
        yield before
    finally:
        after = _snapshot()
        msg: str = (
            f'{label}: '
            f'threads {before[0]}->{after[0]}, '
            f'subints {before[1]}->{after[1]}'
        )
        if writer is None:
            sys.stderr.write(msg + '\n')
            sys.stderr.flush()
        else:
            writer(msg)


def resource_delta_fixture(
    *,
    autouse: bool = True,
    writer: Callable[[str], None] | None = None,

) -> Callable:
    '''
    Factory returning a `pytest` fixture that wraps each test
    in `track_resource_deltas(label=<node.name>)`.

    Usage in a `conftest.py`::

        # tests/conftest.py
        from tractor.devx import resource_delta_fixture

        track_resources = resource_delta_fixture()

    or opt-in per-test::

        track_resources = resource_delta_fixture(autouse=False)

        def test_foo(track_resources):
            ...

    Kept as a factory (not a bare fixture) so callers control
    `autouse` / `writer` without having to subclass or patch.

    '''
    import pytest  # deferred: only needed when caller opts in

    @pytest.fixture(autouse=autouse)
    def _track_resources(request):
        with track_resource_deltas(
            label=request.node.name,
            writer=writer,
        ):
            yield

    return _track_resources
