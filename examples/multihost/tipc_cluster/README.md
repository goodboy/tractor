# `tractor` over `AF_TIPC`, where the address *is* the service name

TIPC is a linux-kernel cluster IPC protocol whose service names
live in a **cluster-wide name table maintained by the kernel**.
For `tractor` that means:

- an actor's IPC address is a service name `(stype, instance)`,
  not a host/port,
- `.bind()`ing it **is** service registration,
- a peer's `.connect()`-by-name **is** the lookup.

So the discovery machinery `tractor.discovery` normally
implements with a registrar actor comes for free, in-kernel —
which is the ask in gh
[#378](https://github.com/goodboy/tractor/issues/378).

> **Why `examples/multihost/`?** `tests/test_docs_examples.py`
> walks `examples/` recursively and runs everything it collects
> as a subproc, asserting `rc == 0`. These need the `tipc`
> kernel module (and, for the two-host pair, a live bearer), so
> they can't satisfy that; `'multihost' not in p[0]` is already
> in the test's exclusion list, which is what keeps them out of
> CI. See "CI" below for the separate matrix-entry plan.

## the single best demo

```bash
sudo modprobe tipc
python single_host.py
```

Four actors boot, four service names appear in the kernel's
table, and all four are withdrawn on teardown — observed with
`tipc(8)`, entirely outside `tractor`:

```
--- `tipc nametable show` :: root + 3 subactors ---
  Type       Lower      Upper      Scope    Port
  1953628160 1616       1616       cluster  3161982128
  1953628160 1219427151 1219427151 cluster  1587358717
  1953628160 2641339936 2641339936 cluster  1864021571
  1953628160 3344505866 3344505866 cluster  3816483388

--- `tipc nametable show` :: after teardown (all withdrawn) ---
  Type       Lower      Upper      Scope    Port
```

`1953628160` is `0x74720000` — `tractor`'s reserved service
type, ascii `tr` in the high half. `1616` is the host-singleton
registrar, the same idiom as the TCP port and the
`registry@1616.sock` UDS filename. The other three instances are
per-actor digests (see "silent crosstalk" below).

## push-based discovery

```bash
python watch_nametable.py
```

Subscribes to the kernel's *topology service* and prints name
table transitions as they happen — no polling, no registrar
round-trip:

```
watching the TIPC name table..
  [+] published  instance=1616         port=0x00000000:2375440573
spawning subactors..
  [+] published  instance=186947472    port=0x00000000:3960753074
  [+] published  instance=2191362136   port=0x00000000:2263898853
  [+] published  instance=3484369663   port=0x00000000:2126817956
tearing down..
  [-] withdrawn  instance=186947472    port=0x00000000:3960753074
  ...
```

This is the groundwork for a push registry in
`tractor.discovery._registry` (gh
[#184](https://github.com/goodboy/tractor/issues/184),
[#216](https://github.com/goodboy/tractor/issues/216)) — a
registrar that *never polls* `find_actor()`.

## two hosts

Everything above is single-node (`modprobe` is enough). To span
hosts you need a **bearer** on both, which is the one thing that
can't be CI'd.

```bash
# on BOTH hosts
sudo modprobe tipc

# over ethernet (L2) — simplest when the hosts share a segment
sudo tipc bearer enable media eth device eth0

# ..or over UDP when L2 isn't available — and MANDATORY over a
# `wg` mesh, see below
sudo tipc bearer enable media udp name uc localip 10.0.11.1

# verify BEFORE running anything: this must list the peer
tipc link list
tipc node list
```

Then:

```bash
# host A
python host_a_srv.py

# host B
python host_b_client.py
```

Note what's absent from both scripts: any IP, hostname or port.
Both sides name the *same service*, and the kernel routes it.
Move `host_a_srv.py` to a third node and host B's dial keeps
working, unchanged.

### over a `wg` mesh

TIPC over WireGuard is the intended reference multihost
deployment (gh #502). One hard constraint: a wg interface is
L3/`tun` — `POINTOPOINT,NOARP`, `link/none`, no L2 address — so
TIPC's `eth` media **cannot** bind it. The udp bearer is
mandatory there, bound to the wg overlay IP, and wg's typical
1420 MTU sits under ethernet's 1500 so link MTU wants checking.

Composed, the deployment address is:

```
/ip4/<pub>/udp/51820/wg/u<key>/tipc/<stype>/<inst>/<scope>
\____ wg bearer ________/\_key_/\______ tractor ep ________/
```

Note the tipc segment has no locative part, unlike tcp's inner
`/ip4/../tcp/..` — a service name is location-independent, so wg
carries the routing and tipc carries identity.

**wg here is not about confidentiality.** TIPC ships AES-GCM
crypto of its own (`tipc node set key`, linux 5.9+) with
cluster/master/per-node keys and rekeying, so "wg adds the
encryption TIPC lacks" is wrong.

The motivation is different but real: TIPC's keys are *symmetric
and pre-shared* — distribution, rotation and revocation are all
on you — whereas wg gives public-key identity and a handshake,
NAT traversal, and one overlay shared by every transport instead
of a TIPC-only mechanism. Worth benchmarking either way; native
crypto skips the tunnel hop.

Note too that the ethernet-bearer pairing #378 imagined does
**not** apply over wg: on a given link the L2 path and the wg
path are mutually exclusive.

### scope

`TIPCAddress._scope` is the backend's `.bindspace` — literally
"the set of hosts this published name is reachable from":

| scope | meaning |
| --- | --- |
| `TIPC_NODE_SCOPE` | same host only — the UDS analogue |
| `TIPC_CLUSTER_SCOPE` | cluster-visible (the default) |

`TIPC_ZONE_SCOPE` is deprecated and aliased to cluster by modern
kernels; `tractor` accepts it on input and folds it, logging at
`transport` level.

## gotchas worth knowing before you deploy

**Silent crosstalk.** Unlike every other backend, a duplicate
bind does **not** raise `EADDRINUSE` — TIPC happily accepts
multiple publishers of one name and *round-robins* connects
between them (verified: 6 dials alternated `b,a,b,a,b,a`). So an
instance collision is silent traffic-splitting, not an error.
That's why `TIPCAddress.get_random()` derives the instance from
a `blake2b` digest of the actor identity rather than a counter.
Two `tractor` trees sharing both a cluster **and** an `_stype`
share a name space; partition them by passing a distinct
`_stype`.

**Graceful close looks like a reset.** A peer closing cleanly
surfaces as `BrokenResourceError`/`ECONNRESET` rather than the
clean 0-byte EOF you get from TCP/UDS. It's benign — the
transport layer already classifies it as a normal disconnect —
but it does look alarming in `transport`-level logs.

**Dialing an unpublished name** answers `EHOSTUNREACH`
*instantly* (no SYN-timeout wait), which is much better
discovery-ping behaviour than TCP. `tractor` normalizes it to
`ConnectionError`.

**It's opt-in, never a default.** The module isn't loaded on
most boxes and doesn't exist off-linux, so
`enable_transports=['tipc']` is always explicit. Check
`tractor.ipc._tipc.is_tipc_available()` before assuming.

## maddr form

There is no registered `/tipc` protocol in the multiaddr table
yet (upstream track: gh
[#483](https://github.com/goodboy/tractor/issues/483) +
multiformats/py-multiaddr#107), so the grammar is interim and
`str`-only:

```
/tipc/<stype>/<instance>/<scope>
```

`parse_maddr()` special-cases this prefix *before* handing
anything to `Multiaddr()`, which would otherwise reject the
unregistered name outright. Registering it upstream is what
would unblock gh
[#443](https://github.com/goodboy/tractor/issues/443)'s
"return `Multiaddr` everywhere" item.

## running the suite over TIPC

The whole test suite runs under the backend:

```bash
sudo modprobe tipc
pytest --tpt-proto tipc
```

Without the module that fails loudly and immediately with an
actionable message rather than a few hundred connect timeouts.
Backend-specific unit tests live in `tests/ipc/test_tipc.py` and
self-skip when the module is absent.

## CI

Single-host TIPC *is* CI-able — the module ships with the
standard Ubuntu kernel package, so a `sudo modprobe tipc` step
plus a `--tpt-proto tipc` matrix entry should work. That's not
wired up yet; verify in a throwaway workflow first, and fall
back to a container job with `--cap-add NET_ADMIN` if the
runners refuse. Cross-node (bearer) testing stays manual — this
README is that smoke test.

## normative refs

The tipc.io docs are stale in places (gh #378 says as much).
Treat the kernel sources as the only normative reference:

- `include/uapi/linux/tipc.h` — address flavours, sockopts, the
  topology `struct`s
- `net/tipc/socket.c`, `net/tipc/topsrv.c`
- `man 8 tipc`
