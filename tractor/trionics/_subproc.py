# tractor: distributed structured concurrency.
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
SC-friendly `trio.run_process()` supervision: a `tn.start()`
style wrapper which surfaces rc!=0 errors deterministically and
(optionally) live-relays the child's std-streams to the `tractor`
log.

'''
from __future__ import annotations
from functools import partial
import subprocess
import textwrap
from typing import (
    Callable,
)

import trio

from ..log import get_logger

log = get_logger()


# sentinel so `supervise_run_process(stdout=...)` can tell
# "caller passed nothing" (-> tty-safe `DEVNULL` default) from
# an explicit `stdout=None` (inherit) override.
_UNSET = object()


def _add_stderr_note(
    cpe: subprocess.CalledProcessError,
    stderr_bytes: bytes,
) -> None:
    '''
    Attach an indented `|_.stderr:` note to a
    `CalledProcessError` for legible rc!=0 reporting at
    teardown.

    '''
    stderr_str: str = stderr_bytes.decode(errors='replace')
    cpe.add_note(
        f'|_.stderr:\n'
        f'{textwrap.indent(stderr_str, prefix=" "*3)}'
    )


async def _relay_stream_lines(
    stream: trio.abc.ReceiveStream,
    *,
    emit: Callable[[str], None]|None = None,
    tag: str = '',
    accum: bytearray|None = None,
) -> None:
    '''
    Concurrently drain a child subproc's `stdout`/`stderr`
    PIPE; relay each COMPLETE line to `emit` (a bound
    `log.<level>` method) prefixed with `tag` (e.g.
    `f'{label}:out'`) and/or append raw bytes to `accum`.

    This reader is MANDATORY whenever a bare
    `stdout=`/`stderr=PIPE` is used WITHOUT `trio`'s
    `capture_*` (which would spawn trio's own internal drain
    task): nothing else drains the OS pipe, so once its kernel
    buffer (~64KiB) fills the child blocks on `write()` ->
    deadlock.

    Modes (combine freely):
    - `emit`-only:  live per-line relay (e.g. `relay_stdout`).
    - `accum`-only: silent drain + capture (e.g. stderr kept
      for a `CalledProcessError` note WITHOUT relaying it).
    - both: relay AND capture (e.g. `relay_stderr` with `check=True`).

    '''
    # NOTE, mirrors `trio._subprocess`'s internal
    # `async with stream: async for ...` drain idiom — except
    # here we EMIT per-line (and/or accumulate) instead of
    # only accumulating.
    residual: bytes = b''
    async with stream:                   # aclose at EOF/cancel
        async for chunk in stream:       # ends at child-exit EOF
            if accum is not None:
                accum += chunk
            if emit is None:
                continue                 # drain(+accum)-only
            buf: bytes = residual + chunk
            *lines, residual = buf.split(b'\n')
            for raw in lines:
                line: str = raw.decode(
                    errors='replace',
                ).rstrip('\r')
                emit(f'[{tag}] {line}')

    # flush any trailing partial (un-newline-term'd) line @ EOF
    if (
        emit is not None
        and
        residual
    ):
        line: str = residual.decode(
            errors='replace',
        ).rstrip('\r')
        emit(f'[{tag}] {line}')


async def supervise_run_process(
    cmd: list[str]|str,
    *,
    check: bool = True,
    label: str|None = None,

    # per-line `log.*` relay of the child's std-streams
    # (tty-safe, capture-safe, STREAMED — not
    # buffered-until-exit, so it suits long-lived daemons).
    relay_stdout: bool = False,
    relay_stderr: bool = False,

    # default `io` (our custom level, value 21): the relay
    # exists to make windowless-spawn output VISIBLE, and
    # `IO`(21) sorts just ABOVE `INFO`(20) so it shows at the
    # usual `info`/`devx` console levels (a `runtime`(15) relay
    # would be silently filtered) while staying distinctly
    # labelled + separately filterable.
    relay_level: str = 'io',

    # non-relay `stdout` override; defaults (via `_UNSET`) to
    # `DEVNULL` so we NEVER inherit (+ thus can't clobber) the
    # parent controlling-tty.
    stdout: int = _UNSET,

    task_status: trio.TaskStatus[
        trio.Process
    ] = trio.TASK_STATUS_IGNORED,

    # any other `trio.run_process()` kwarg (env, shell, cwd,
    # start_new_session, executable, ...) forwarded verbatim;
    # our MANAGED keys (stdin/stdout/stderr/check) are set
    # below and WIN on conflict.
    **run_process_kwargs,
) -> None:
    '''
    A `trio.Nursery.start()`-style `trio.run_process()`
    wrapper which,

    - surfaces a rc!=0 `subprocess.CalledProcessError`
      DETERMINISTICALLY: we pass `check=False` to `trio` and
      do our OWN post-drain rc-check, (re)building + raising a
      BARE CPE (with a `.stderr` note) from this coro's body
      AFTER the child exits — so there's no nursery-eg-wrapped
      CPE to catch/`collapse_eg`, and the relay reader is never
      race-cancelled mid-drain.

    - ALWAYS isolates the parent controlling-tty
      (`stdin=DEVNULL`, and `stdout=DEVNULL` unless
      relayed/overridden) so a spawned program can't emit
      terminal control-seqs onto the launching tty (which
      would clobber its scrollback).

    - optionally live-relays `stdout`/`stderr` per-line to
      `log.<relay_level>` via concurrent reader tasks (see
      `_relay_stream_lines`).

    Delivers the live `trio.Process` via
    `task_status.started()` then SUPERVISES it (the
    `run_process` bg task + any relay readers) to completion
    in this coro — i.e. the parent `tn.start()` returns
    immediately/non-blocking.

    NOTE: any crash-handling / `repl_fixture` layer is
    intentionally NOT baked in here — compose it ON TOP at the
    call-site, e.g.

        async with maybe_open_crash_handler():
            await tn.start(
                partial(supervise_run_process, cmd, ...),
            )

    '''
    emit: Callable[[str], None] = getattr(log, relay_level)
    tag: str = (
        label
        or
        (cmd if isinstance(cmd, str) else ' '.join(cmd))
    )

    # forward any extra `trio.run_process` kwargs verbatim;
    # MANAGED keys below override on conflict.
    rp_kwargs: dict = dict(run_process_kwargs)

    # XXX ALWAYS isolate the controlling-tty's stdin.
    rp_kwargs['stdin'] = subprocess.DEVNULL

    # stdout: relay -> our own PIPE (drained by the reader
    # below); else an explicit override; else tty-safe
    # `DEVNULL`.
    if relay_stdout:
        rp_kwargs['stdout'] = subprocess.PIPE
    elif stdout is not _UNSET:
        rp_kwargs['stdout'] = stdout
    else:
        rp_kwargs['stdout'] = subprocess.DEVNULL

    # stderr: PIPE (+ our reader) when we either RELAY it OR
    # need it captured for a rc!=0 CPE note; else tty-safe
    # `DEVNULL`. We accumulate ONLY when `check` (the note is
    # the only consumer).
    #
    # XXX we ALWAYS pass `check=False` to `trio` and do our
    # OWN deterministic post-drain rc-check (below) so `trio`
    # never raises a nursery-eg-wrapped CPE — no `collapse_eg`
    # workaround, no reader race-cancel.
    want_stderr_pipe: bool = relay_stderr or check
    stderr_accum: bytearray|None = bytearray() if check else None
    rp_kwargs['check'] = False
    rp_kwargs['stderr'] = (
        subprocess.PIPE if want_stderr_pipe
        else subprocess.DEVNULL
    )

    async with trio.open_nursery() as own_tn:
        trio_proc: trio.Process = await own_tn.start(
            partial(
                trio.run_process,
                cmd,
                **rp_kwargs,
            )
        )

        # spin up the concurrent pipe-drain relay reader(s) —
        # see `_relay_stream_lines` for why these are mandatory
        # (not cosmetic) when piping without `capture_*`.
        if relay_stdout:
            own_tn.start_soon(
                partial(
                    _relay_stream_lines,
                    trio_proc.stdout,
                    emit=emit,
                    tag=f'{tag}:out',
                )
            )
        if want_stderr_pipe:
            own_tn.start_soon(
                partial(
                    _relay_stream_lines,
                    trio_proc.stderr,
                    # relay live only if asked; else silent
                    # drain+capture for the CPE note.
                    emit=emit if relay_stderr else None,
                    tag=f'{tag}:err',
                    accum=stderr_accum,
                )
            )

        # hand the live proc up to the parent WITHOUT blocking
        # on the bg supervise/relay tasks (keeps non-blocking
        # `tn.start()` semantics).
        task_status.started(trio_proc)

    # ===== deterministic post-drain rc-check (BOTH paths) =====
    # `own_tn` only unwinds once `run_process` AND the relay
    # reader(s) have hit EOF + FULLY drained — so `stderr_accum`
    # is COMPLETE here (no race vs an early CPE-cancel). Rebuild
    # + raise a BARE `CalledProcessError` (the parent `tn` will
    # eg-wrap it like any task-raise; callers `collapse_eg()` if
    # they want it bare).
    if (
        check
        and
        trio_proc.returncode
    ):
        stderr_bytes: bytes = (
            bytes(stderr_accum)
            if stderr_accum is not None
            else b''
        )
        cpe = subprocess.CalledProcessError(
            returncode=trio_proc.returncode,
            cmd=trio_proc.args,
            stderr=stderr_bytes,
        )
        _add_stderr_note(cpe, stderr_bytes)
        raise cpe
