# `infect_asyncio` × `main_thread_forkserver` Mode-A deadlock

## Reproducer

```bash
./py313/bin/python -m pytest \
  tests/test_infected_asyncio.py::test_aio_simple_error \
  --tpt-proto=tcp \
  --spawn-backend=main_thread_forkserver \
  -v --capture=sys
```

Hangs indefinitely. Mode-A signature — both processes
parked in `epoll_wait`, **neither burning CPU**.

## Empirical observations (caught alive)

### Outer pytest (parent)

`py-spy dump` on the test runner pid shows the trio
event loop parked at the bottom of `trio.run()`:

```
Thread <pid> (idle): "MainThread"
    get_events (trio/_core/_io_epoll.py:245)
        self: <EpollIOManager at 0x...>
        timeout: 86400
    run (trio/_core/_run.py:2415)
        next_send: []
        timeout: 86400
    test_aio_simple_error (tests/test_infected_asyncio.py:175)
```

`timeout: 86400` is trio's "no scheduled work, just wait
for I/O forever" sentinel. `next_send: []` confirms
nothing is queued. The parent is stuck inside
`tractor.open_nursery(...).run_in_actor(...)` waiting
for `ipc_server.wait_for_peer(uid)` to fire — i.e.
waiting for the spawned subactor to connect back.

### Subactor (forked child)

`/proc/<pid>/stack`:

```
do_epoll_wait+0x4c0/0x500
__x64_sys_epoll_wait+0x70/0x120
do_syscall_64+0xef/0x1540
entry_SYSCALL_64_after_hwframe+0x77/0x7f
```

`strace -p <pid> -f`:

```
[pid <child-A>] epoll_wait(6 <unfinished ...>
[pid <child-B>] epoll_wait(3
```

**Two threads**, both parked in `epoll_wait` on
distinct epoll fds. Both blocked, neither making
progress.

### Subactor file-descriptor table

```
fd=0,1,2     stdio
fd=3         eventpoll [watches fd 4]
fd=4 ↔ fd=5  unix STREAM (CONNECTED) — internal pair
fd=6         eventpoll [watches fds 7, 9]
fd=7 ↔ fd=8  unix STREAM (CONNECTED) — internal pair
fd=9 ↔ fd=10 unix STREAM (CONNECTED) — internal pair
```

Confirmed via `ss -xp` peer-inode lookup: **all 6 unix
sockets are internal socketpairs** (peer in same pid).

**Critical**: zero TCP/IPv4/IPv6 sockets, despite
`--tpt-proto=tcp`:

```
$ sudo lsof -p <subactor> | grep -iE 'TCP|IPv'
(empty)
$ sudo ss -tnp | grep <subactor>
(empty)
```

**The subactor never opened a TCP connection back to
the parent.**

## Diagnosis

The subactor reaches `_actor_child_main` →
`_trio_main(actor)` →
`run_as_asyncio_guest(trio_main)`. Code path
(`tractor.spawn._entry`):

```python
if infect_asyncio:
    actor._infected_aio = True
    run_as_asyncio_guest(trio_main)   # ←  this branch
else:
    trio.run(trio_main)
```

`run_as_asyncio_guest` (`tractor.to_asyncio`):

```python
def run_as_asyncio_guest(trio_main, ...):
    async def aio_main(trio_main):
        loop = asyncio.get_running_loop()
        trio_done_fute = asyncio.Future()
        ...
        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )
        out = await asyncio.shield(trio_done_fute)
        return out.unwrap()
    ...
    return asyncio.run(aio_main(trio_main))
```

Expected flow:
1. `asyncio.run(aio_main(...))` — boots fresh asyncio
   loop in calling thread.
2. `aio_main` calls `trio.lowlevel.start_guest_run(...)`
   — initializes trio's I/O manager, schedules first
   trio slice via `loop.call_soon_threadsafe`.
3. asyncio loop dispatches the callback → trio runs a
   slice → yields back via `call_soon_threadsafe`.
4. Trio's `async_main` (the user function) runs →
   `Channel.from_addr(parent_addr)` → TCP connect to
   parent.

What we observe instead:
- 2 threads in `epoll_wait` (one trio epoll, one
  asyncio epoll, both inactive)
- 6 unix-socket fds (3 socketpairs: trio
  wakeup-fd-pair, asyncio wakeup-fd-pair, trio kicker
  socketpair)
- ZERO TCP — `Channel.from_addr` never ran

Most likely cause: **trio's guest-run scheduling
callback didn't get dispatched by asyncio's loop in
the forked child**, so trio's `async_main` never
executes past trio bootstrap, and the
parent-IPC-connect step is never reached.

## Fork-survival risk surface (hypothesis)

`trio.lowlevel.start_guest_run` builds Python-level
closures + signal handlers + wakeup-fd registrations
that depend on:

- The asyncio event loop's `call_soon_threadsafe`
  thread-id matching the loop owner thread.
- Process-wide signal-wakeup-fd state
  (`signal.set_wakeup_fd`).
- Trio's `KIManager` SIGINT handler.

Under `main_thread_forkserver`, the fork happens from
a worker thread that has **never entered trio**
(intentional — trio-free launchpad). But the FORKED
child then tries to bring up BOTH asyncio AND
trio-as-guest fresh from this trio-free thread. The
asyncio loop boots fine; trio's `start_guest_run`
initializes BUT the cross-loop dispatch (asyncio
queue → trio slice) appears to silently fail to wire
up.

Two more hypotheses worth probing:

1. **Wakeup-fd contention**: asyncio installs
   `signal.set_wakeup_fd(<own_pair>)`. trio's
   guest-run also wants a wakeup-fd. Whoever installs
   second wins; the loser's `epoll_wait` no longer
   wakes on signals. Combined with the `asyncio.shield(
   trio_done_fute)` + `asyncio.CancelledError`
   handling in `run_as_asyncio_guest`, a missed signal
   delivery could explain the indefinite park.

2. **Trio kicker socketpair race**: trio's I/O manager
   uses an internal `socket.socketpair()` to "kick"
   itself out of `epoll_wait` when a non-IO task needs
   scheduling. In guest mode, the kicker is still
   present but is supposed to be triggered via the
   asyncio dispatch. If the kicker write never gets
   issued by asyncio's callback, trio's epoll never
   wakes.

## Confirmed via py-spy (live capture)

After detaching `strace` (ptrace is exclusive — that's
why `py-spy` returns EPERM if strace is attached):

```
Thread <pid> (idle): "main-thread-forkserver[asyncio_actor]"
    select (selectors.py:452)                          # asyncio epoll
    _run_once (asyncio/base_events.py:2012)
    run_forever (asyncio/base_events.py:683)
    run_until_complete (asyncio/base_events.py:712)
    run (asyncio/runners.py:118)
    run (asyncio/runners.py:195)
    run_as_asyncio_guest (tractor/to_asyncio.py:1770)
    _trio_main (tractor/spawn/_entry.py:160)
    _actor_child_main (tractor/_child.py:72)
    _child_target (tractor/spawn/_main_thread_forkserver.py:910)
    _worker (tractor/spawn/_main_thread_forkserver.py:605)
    [thread bootstrap]

Thread <pid+1> (idle): "Trio thread 14"
    get_events (trio/_core/_io_epoll.py:245)           # trio epoll
    get_events (trio/_core/_run.py:1678)
    capture (outcome/_impl.py:67)
    _handle_job (trio/_core/_thread_cache.py:173)
    _work (trio/_core/_thread_cache.py:196)
    [thread bootstrap]
```

This data **rewrites the diagnosis**: trio guest-run
isn't broken across the fork — it's working as designed.
The two threads ARE the canonical guest-run architecture:

1. **Asyncio main loop** runs in the lead thread. Parked
   in `selectors.EpollSelector.select(timeout=-1)` —
   waiting indefinitely for ANY callback to be queued.
2. **Trio's I/O manager** offloads `get_events`
   (`epoll_wait`) onto a `trio._core._thread_cache`
   worker thread. The worker calls
   `outcome.capture(get_events)` and parks in
   `epoll_wait(timeout=86400)`.
3. When trio I/O fires (or its kicker socketpair gets a
   write), the worker returns from `epoll_wait`,
   delivers the result via `_handle_job`'s `deliver`
   callback, which schedules the next trio slice on
   asyncio via `loop.call_soon_threadsafe`.

The fact that the trio thread is *already* in
`_thread_cache._handle_job` doing `capture(get_events)`
means **trio's scheduler HAS started** — the bridge
asyncio↔trio is wired correctly post-fork.

So `async_main` DID run far enough to register some
trio task that's now awaiting I/O. The question
becomes: **what is `async_main` waiting on?**

Process state confirms it's NOT waiting on the TCP
connect to parent:

```
$ sudo lsof -p <subactor> | grep -iE 'TCP|IPv'
(empty)
$ sudo ss -tnp | grep <subactor>
(empty)
```

`Channel.from_addr(parent_addr)` — the very first
thing `async_main` does — was never reached, OR was
reached but errored before `socket()` was called. The
parent (running `ipc_server.wait_for_peer`) waits
forever for the connection; it never comes.

## Refined hypothesis

`async_main` is stalled in some PRE-`Channel.from_addr`
checkpoint. Candidates:

1. **`get_console_log` / logger init** — called early in
   `_trio_main` if `actor.loglevel is not None`. Logging
   setup involves file/handler init that could block on
   something fork-inherited (e.g. a stale lock).
2. **`debug.maybe_init_greenback`** — `start_guest_run`
   includes a check (`if debug_mode(): assert 0` —
   currently asserts unsupported). For non-debug mode
   this is bypassed but related machinery may run.
3. **Stackscope SIGUSR1 handler install** — gated on
   `_debug_mode` OR `TRACTOR_ENABLE_STACKSCOPE` env-var.
   The `enable_stack_on_sig()` path captures a trio
   token via `trio.lowlevel.current_trio_token()` —
   could block under guest mode.
4. **Initial `await trio.sleep(0)` / first checkpoint**
   in `async_main` before reaching the
   `Channel.from_addr` line. Under guest mode, if the
   FIRST `call_soon_threadsafe` callback never gets
   processed by asyncio, trio's first slice never
   completes — but the worker thread WOULD still be in
   `epoll_wait` having been started by trio's I/O
   manager init.

## Confirming `async_main`'s parked location

Add temporary logging at the top of `Actor.async_main`:

```python
# tractor/runtime/_runtime.py around line 855
async def async_main(self, parent_addr=None):
    log.devx('async_main: ENTERED')                # marker A
    try:
        log.devx('async_main: pre-Channel.from_addr')  # marker B
        chan = await Channel.from_addr(
            addr=wrap_address(parent_addr)
        )
        log.devx('async_main: post-Channel.from_addr')  # marker C
        ...
```

Re-run the test with `--ll=devx`. The last marker logged
tells us exactly where `async_main` parked. If only A
fires, the issue is between A and B (logger init,
stackscope, etc.). If A and B fire but not C, it's in
`Channel.from_addr` (DNS, socket creation, connect).

## Related sibling bug

`tests/test_multi_program.py::test_register_duplicate_name`
hangs under the same backend with a DIFFERENT
fingerprint:

- Subactor at 100% CPU (busy-loop), not parked
- `recvfrom(6, "", 65536, 0, NULL, NULL) = 0` repeating
  with no `epoll_wait` in between
- fd=6 is one of trio's internal AF_UNIX
  socketpair fds (the kicker mechanism)

Distinct root cause — possibly trio's kicker socketpair
inheriting a half-closed state across the fork — but
shares the broader theme: **trio internal-state
initialization isn't fully fork-safe under
`main_thread_forkserver`** for the more exotic
dispatch paths.

## Workarounds (until fix lands)

1. **Skip-mark on the fork backend** — temporarily mark
   `tests/test_infected_asyncio.py` with
   `pytest.mark.skipon_spawn_backend('main_thread_forkserver',
   reason='infect_asyncio + fork interaction broken,
   see ai/conc-anal/infected_asyncio_under_main_thread_forkserver_hang_issue.md')`.
   Lets the rest of the test suite run green while
   this is being fixed properly.

2. **Run infected-asyncio tests under the `trio`
   backend only** — they don't exercise fork
   semantics, so they won't hit this bug.

## Investigation next steps

In rough priority:

1. Catch the hang alive again, **detach strace**,
   `py-spy --locals` the subactor — confirm trio
   thread is NOT yet at `async_main`.
2. Diff `start_guest_run` setup pre-fork vs post-fork
   by adding `log.devx()` markers in
   `tractor.to_asyncio.run_as_asyncio_guest::aio_main`
   at:
   - asyncio loop bringup
   - immediately before `start_guest_run`
   - immediately after `start_guest_run`
   - inside the `trio_done_callback` registration
3. Check whether the asyncio loop dispatches ANY
   callbacks in the forked child — instrument
   `loop.call_soon_threadsafe` (e.g. monkey-patch
   `loop._call_soon` to log).
4. If steps 1–3 confirm that asyncio's queue is
   stuck, look at whether the asyncio event-loop
   policy or selector is being inherited from a
   pre-fork (parent-process) state in a way that
   breaks the new loop.

## See also

- [#379](https://github.com/goodboy/tractor/issues/379) — subint umbrella
- [#451](https://github.com/goodboy/tractor/issues/451) — Mode-A cancel-cascade hang
- `ai/conc-anal/fork_thread_semantics_execution_vs_memory.md`
- `ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`
- python-trio/trio#1614 — trio + fork hazards
