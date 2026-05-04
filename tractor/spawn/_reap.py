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
Post-mortem subactor cleanup primitives — things the parent
runtime has to clean up because the dead-or-SIGKILL'd child
couldn't.

Sibling of `tractor._testing._reap` which is the test-harness
equivalent (orphan-pid + leaked-shm + leaked-UDS-sock sweeper
fixtures). This module is the spawn-layer counterpart, called
inline from `hard_kill` and the broader subactor reap path.

Today this is just `unlink_uds_bind_addrs()`. As future
post-mortem cleanup needs surface (e.g. `/dev/shm` segment
unlink for hard-crashed actors, leaked-pidfile cleanup), they
land here too.

Future-work TODO — authoritative UDS bind-addr tracking
-------------------------------------------------------

`unlink_uds_bind_addrs()` currently has two cleanup paths:

1. Explicit `bind_addrs` (when parent set them at spawn time)
2. **Convention-based reconstruction** —
   `<XDG_RUNTIME_DIR>/tractor/<name>@<pid>.sock` — for the
   common case where the subactor self-assigned a random sock
   via `UDSAddress.get_random()`.

Path (2) hardcodes the `<name>@<pid>.sock` convention from
`tractor.ipc._uds.UDSAddress`. If that convention ever
changes — or the subactor binds to a non-default
`bindspace`/`filedir` — we'll silently fail to unlink.

A more authoritative approach would be:

- Subactors register their bound UDS sockpaths in a
  per-process registry inside `tractor.ipc._uds` at
  `start_listener()` time.
- The subactor reports its bound sockpath(s) back to the
  parent over IPC immediately post-bind (extension to
  `SpawnSpec` reply / a new handshake msg).
- Parent caches the subactor's authoritative sockpaths.
- `unlink_uds_bind_addrs()` checks the cache FIRST, falls
  back to convention-reconstruction if the subactor died
  before reporting (which is the SIGKILL case this fn
  primarily exists for).

Tracked as future work in #454 (the parent UDS-leak
issue this module addresses); a separate issue may be
filed if/when the registry impl is scoped.

See also #452 — the discovery-client `CLOSE_WAIT` TCP
fd leak. Different bug class but same broader theme of
"fork-spawn unmasked latent cleanup gaps".

'''
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import trio

from tractor.discovery._addr import (
    UnwrappedAddress,
    wrap_address,
)
from tractor.ipc._uds import UDSAddress
from tractor.log import get_logger


if TYPE_CHECKING:
    from tractor.runtime._runtime import Actor


log = get_logger('tractor')


def unlink_uds_bind_addrs(
    proc: trio.Process,
    *,
    bind_addrs: list[UnwrappedAddress] | None = None,
    subactor: Actor | None = None,
) -> None:
    '''
    Best-effort post-mortem cleanup of any UDS sock-files
    a hard-killed subactor was bound to.

    SIGKILL bypasses Python execution → the subactor's
    `_serve_ipc_eps` `finally:` block (which normally calls
    `os.unlink(addr.sockpath)`) never runs. Without this
    parent-side cleanup, the dead subactor's
    `${XDG_RUNTIME_DIR}/tractor/<name>@<pid>.sock` file
    accumulates on the filesystem (see issue #454 + the
    autouse `_track_orphaned_uds_per_test` fixture).

    Two cleanup paths, in order:

    1. **Explicit `bind_addrs`** — when the parent set the
       subactor's bind addrs at spawn time, unlink each
       UDS-flavored sockpath directly.
    2. **Self-assigned reconstruction** — when
       `bind_addrs` is empty (the common case: subactor
       picked its own random sock via
       `UDSAddress.get_random()`), reconstruct the path
       from `(subactor.aid.name, proc.pid)` using the
       same `<name>@<pid>.sock` convention. We can do this
       because the subactor uses its OWN `os.getpid()` at
       bind time, which equals `proc.pid` from the
       parent's view.

    Idempotent: `FileNotFoundError` (graceful exit
    already-unlinked, or sock never bound under early-
    spawn cancel) is silenced; other `OSError`s log a
    warning but never raise. TCP / non-UDS bind addrs are
    skipped.

    '''
    sockpaths: list[str] = []

    # path 1: explicit bind_addrs set at spawn time
    for unwrapped in (bind_addrs or ()):
        try:
            addr = wrap_address(unwrapped)
        except Exception:
            log.exception(
                f'Failed to wrap addr for UDS post-kill cleanup '
                f'— skipping {unwrapped!r}\n'
            )
            continue
        if isinstance(addr, UDSAddress):
            sockpaths.append(str(addr.sockpath))

    # path 2: reconstruct from subactor name + proc pid
    # for the random-self-assign case (bind_addrs=None)
    #
    # TODO authoritative tracking — see module docstring.
    if (
        not sockpaths
        and subactor is not None
        and proc.pid is not None
    ):
        sockname: str = f'{subactor.aid.name}@{proc.pid}.sock'
        sockpath: str = str(
            UDSAddress.def_bindspace / sockname
        )
        sockpaths.append(sockpath)

    for sockpath in sockpaths:
        try:
            os.unlink(sockpath)
            log.runtime(
                f'Unlinked orphaned UDS sock-file post-SIGKILL\n'
                f' |_{proc}\n'
                f' |_{sockpath}\n'
            )
        except FileNotFoundError:
            # raced — subactor cleaned up before SIGKILL,
            # OR sockfile never bound (early-spawn cancel),
            # OR transport wasn't UDS this run.
            pass
        except OSError as exc:
            log.warning(
                f'Failed to unlink subactor UDS sock-file '
                f'post-SIGKILL\n'
                f' |_{proc}\n'
                f' |_{sockpath}\n'
                f' |_{exc!r}\n'
            )
