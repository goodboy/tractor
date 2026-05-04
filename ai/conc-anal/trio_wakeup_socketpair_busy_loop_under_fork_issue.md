# trio `WakeupSocketpair.drain()` busy-loop in forked child (peer-closed missed-EOF)

## Reproducer

```bash
./py313/bin/python -m pytest \
  tests/test_multi_program.py::test_register_duplicate_name \
  --tpt-proto=tcp \
  --spawn-backend=main_thread_forkserver \
  -v --capture=sys
```

Subactor pegs a CPU core indefinitely; parent test
hangs waiting for the subactor.

## Empirical evidence (caught alive)

```
$ sudo strace -p <subactor-pid>
recvfrom(6, "", 65536, 0, NULL, NULL)   = 0
recvfrom(6, "", 65536, 0, NULL, NULL)   = 0
recvfrom(6, "", 65536, 0, NULL, NULL)   = 0
... (no `epoll_wait`, no other syscalls, just this back-to-back)
```

Pattern: tight C-level `recvfrom` loop returning 0
each call. No `epoll_wait` between iterations →
**not trio's task scheduler**. Pure synchronous C
loop.

```
$ sudo readlink /proc/<subactor-pid>/fd/6
socket:[<inode>]

$ sudo lsof -p <subactor-pid> | grep ' 6u'
<cmd> <pid> goodboy 6u unix 0xffff... 0t0 <inode> type=STREAM (CONNECTED)
```

fd=6 is an **AF_UNIX socket** in CONNECTED state.
Even though the test uses `--tpt-proto=tcp`, this fd
is NOT a tractor IPC channel — it's an internal
trio socketpair.

## Root-cause: `WakeupSocketpair.drain()`

`/site-packages/trio/_core/_wakeup_socketpair.py`:

```python
class WakeupSocketpair:
    def __init__(self) -> None:
        self.wakeup_sock, self.write_sock = socket.socketpair()
        self.wakeup_sock.setblocking(False)
        self.write_sock.setblocking(False)
        ...

    def drain(self) -> None:
        try:
            while True:
                self.wakeup_sock.recv(2**16)
        except BlockingIOError:
            pass
```

`socket.socketpair()` on Linux defaults to AF_UNIX
SOCK_STREAM. Both ends non-blocking. Normal flow:

1. Signal/wake event → `write_sock.send(b'\x00')`
   queues a byte.
2. `wakeup_sock` becomes readable → trio's epoll
   triggers.
3. Trio calls `drain()` to flush the buffer.
4. drain loops on `wakeup_sock.recv(64KB)`.
5. Eventually buffer empty → non-blocking socket
   raises `BlockingIOError` → except → break.

**Bug surface — peer-closed missed-EOF**:

Non-blocking socket semantics:
- buffer has data → `recv` returns N>0 bytes (loop continues)
- buffer empty → `recv` raises `BlockingIOError`
- **peer FIN'd → `recv` returns 0 bytes (NEITHER exception NOR
  break — infinite tight loop)**

`drain()` does not handle the `b''` return-value
(EOF) case. If `write_sock` has been closed (or the
process holding it is gone), every iteration returns
0 → infinite loop → 100% CPU on a single core.

## Why this triggers under `main_thread_forkserver`

Under `os.fork()` from the forkserver-worker thread:

1. Parent has a `WakeupSocketpair` instance with
   `wakeup_sock=fdN`, `write_sock=fdM`. Both fds
   open in parent.
2. Fork → child inherits BOTH fds (kernel-level fd
   table dup).
3. `_close_inherited_fds()` runs in child →
   closes everything except stdio. `wakeup_sock` and
   `write_sock` of the parent's `WakeupSocketpair`
   ARE closed in child.
4. Child's trio (running fresh) creates its OWN
   `WakeupSocketpair` → NEW fd numbers (e.g. fd 6, 7).
5. **In `infect_asyncio` mode** the asyncio loop is
   the host; trio runs as guest via
   `start_guest_run`. trio still creates its
   `WakeupSocketpair` in the I/O manager but its
   role is different.

The race window: somewhere between (3) and (5), if a
`WakeupSocketpair` Python object reference inherited
via COW (from parent's pre-fork heap) survives long
enough that `drain()` is called on it AFTER its fds
were closed but BEFORE the child's NEW socketpair
takes over the recycled fd numbers — the recycled fd
will be one of the child's NEW socketpair ends, whose
peer might be FIN-flagged (e.g. parent-process
peer-end is closed).

Or simpler: the `wait_for_actor`/`find_actor` discovery
flow in `test_register_duplicate_name` triggers an
unusual code path where a stale `WakeupSocketpair`
gets `drain()`-called on a fd whose peer has already
closed.

## Why `drain()` shouldn't loop indefinitely on EOF
(upstream trio bug)

Even WITHOUT fork, `drain()` should treat `b''` as
EOF and break. The current code is correct for the
"buffer drained on a healthy socketpair" scenario but
incorrect for the "peer is gone" scenario. It's a
defensive-programming gap in trio.

A one-line patch upstream:

```python
def drain(self) -> None:
    try:
        while True:
            data = self.wakeup_sock.recv(2**16)
            if not data:
                break  # peer-closed; nothing more to drain
    except BlockingIOError:
        pass
```

## Workarounds (until the underlying issue lands)

1. **Skip-mark on the fork backend**:
   `tests/test_multi_program.py` →
   `pytest.mark.skipon_spawn_backend('main_thread_forkserver',
   reason='trio WakeupSocketpair.drain busy-loop, see ai/conc-anal/trio_wakeup_socketpair_busy_loop_under_fork_issue.md')`.

2. **Defensive monkey-patch in tractor's
   forkserver-child prelude** — wrap
   `WakeupSocketpair.drain` to handle `b''`:

   ```python
   # in `_actor_child_main` or `_close_inherited_fds`'s
   # post-fork prelude:
   from trio._core._wakeup_socketpair import WakeupSocketpair
   _orig_drain = WakeupSocketpair.drain
   def _safe_drain(self):
       try:
           while True:
               data = self.wakeup_sock.recv(2**16)
               if not data:
                   return  # peer closed
       except BlockingIOError:
           pass
   WakeupSocketpair.drain = _safe_drain
   ```

   Tracks upstream — remove once trio fixes.

3. **Upstream the fix**: 1-line PR to `python-trio/trio`
   adding `if not data: break` to `drain()`.

## Investigation next steps

1. **Confirm via py-spy**: when caught alive, detach
   strace first then
   `sudo py-spy dump --pid <subactor> --locals`. The
   busy thread should show `drain` from `WakeupSocketpair`
   in the call chain.
2. **Identify which write-end peer is closed**: from
   the inode of fd 6, look up the matching peer
   inode via `ss -xp` and see whose process it
   was/is.
3. **Verify the missed-EOF hypothesis**: hand-craft a
   minimal `WakeupSocketpair` repro:

   ```python
   from trio._core._wakeup_socketpair import WakeupSocketpair
   ws = WakeupSocketpair()
   ws.write_sock.close()  # simulate peer-gone
   ws.drain()             # should hang forever
   ```

## Sibling bug

`tests/test_infected_asyncio.py::test_aio_simple_error`
hangs under the same backend with a DIFFERENT
fingerprint (Mode-A deadlock, both parties in
`epoll_wait`, no busy-loop). Distinct root cause —
see `infected_asyncio_under_main_thread_forkserver_hang_issue.md`.

Both share the broader theme: **trio internal-state
initialization isn't fully fork-safe under
`main_thread_forkserver`** for the more exotic
dispatch paths.

## See also

- [#379](https://github.com/goodboy/tractor/issues/379) — subint umbrella
- python-trio/trio#1614 — trio + fork hazards
- `trio._core._wakeup_socketpair.WakeupSocketpair`
  source (the smoking gun)
- `ai/conc-anal/fork_thread_semantics_execution_vs_memory.md`
- `ai/conc-anal/infected_asyncio_under_main_thread_forkserver_hang_issue.md`
