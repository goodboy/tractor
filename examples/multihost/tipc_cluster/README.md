# `tractor` over `AF_TIPC`, where the address *is* the service name

TIPC is a linux-kernel cluster IPC protocol whose service names
live in a **cluster-wide name table maintained by the kernel**.
It is also described as **Cluster Domain Sockets**: the Unix-domain
socket model extended from one kernel to a cluster. That name is a
useful explanation for new users, while the code keeps `tipc` as its
protocol key to match Linux's `AF_TIPC`, kernel module and tooling.
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

For the first physical test, use two wired Linux hosts on the same
L2 segment. Prefer a direct cable or uncomplicated switch; avoid
Wi-Fi, guest VLANs and port isolation until the basic link works.
Use the same checkout and Python environment on both hosts:

```bash
# on BOTH hosts
git rev-parse HEAD             # must match on A and B
uv sync --all-extras --dev
sudo modprobe tipc

# choose the real wired iface; do not assume `eth0`
ip -br link
IFACE=enp3s0

# inspect existing cluster identity before changing anything
tipc node get address          # must differ between hosts
tipc node get netid            # must match between hosts

# use one private test netid on BOTH hosts, before enabling bearers
sudo tipc node set netid 37801

# ethernet is simplest when the hosts share an L2 segment
sudo tipc bearer enable media eth device "$IFACE"

# ..or over UDP when L2 isn't available — and MANDATORY over a
# `wg` mesh, see below
sudo tipc bearer enable media udp name uc localip 10.0.11.1

# verify BEFORE running anything: this must list the peer
tipc bearer list
tipc link list
tipc node list
```

If the link does not appear, first verify carrier, a common TIPC
network ID, distinct node addresses, a common VLAN and compatible
MTUs. Ethernet TIPC uses EtherType traffic rather than IP routing,
so a successful `ping` alone does not prove the bearer can work.

Run each command from `examples/multihost/tipc_cluster/`:

```bash
# host A
uv run python host_a_srv.py

# host B
watch -n 0.5 tipc nametable show  # optional second terminal
uv run python host_b_client.py
```

Note what's absent from both scripts: any IP, hostname or port.
Both sides name the *same service*, and the kernel routes it.
Move `host_a_srv.py` to a third node and host B's dial keeps
working, unchanged.

For a first resilience pass, use a local console or separate
management link so the test does not cut off your own SSH session:

```bash
# host B: record the healthy baseline
tipc link list
tipc link statistics show

# either host: withdraw and recreate the bearer
sudo tipc bearer disable media eth device "$IFACE"
tipc link list
sudo tipc bearer enable media eth device "$IFACE"
tipc link list

# prove name withdrawal/republication and RPC recovery
tipc nametable show
uv run python host_b_client.py
```

Capture `uname -a`, both node addresses, `tipc bearer list`,
`tipc link list`, `tipc link statistics show`, the name table and
both Python transcripts. Those artifacts distinguish an actor bug
from bearer discovery, cluster identity or switch configuration.

Clean up a disposable Ethernet test on both hosts with:

```bash
sudo tipc bearer disable media eth device "$IFACE"
tipc link list
```

Restore any pre-existing network ID only after all bearers are
disabled. The first useful automation target is a two-node network
namespace fixture that asserts link-up, remote publication, RPC,
withdrawal and republication in that order; physical hardware then
remains the validation layer for real NIC and switch behaviour.

The commands above use iproute2's `tipc` frontend, which speaks the
kernel's `TIPCv2` generic-netlink family. The planned `pyroute2`
dependency already manages WireGuard, interfaces and namespaces and
provides generic-netlink primitives, but it does not currently ship a
TIPC message codec. Adding one upstream would let `tractor` replace
these manual commands with one Python netlink stack instead of
shelling out; until then, `tipc(8)` remains the canonical frontend.

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
standard Ubuntu kernel package. CI loads it with `sudo modprobe
tipc` and runs the suite with `--tpt-proto tipc` as a blocking
matrix leg. Cross-node bearer testing stays manual — this README
is that smoke test.

## normative refs

The tipc.io docs are stale in places (gh #378 says as much).
Treat the kernel sources as the only normative reference:

- `include/uapi/linux/tipc.h` — address flavours, sockopts, the
  topology `struct`s
- `net/tipc/socket.c`, `net/tipc/topsrv.c`
- `man 8 tipc`
