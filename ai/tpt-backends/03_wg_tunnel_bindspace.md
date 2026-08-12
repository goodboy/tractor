# Plan 03 — WireGuard (and other tunnels) as a *nested bindspace* via `pyroute2`

Tracks gh [#482] + the tunnelled-maddr item of [#443].
Prereq reading:
[`00_shared_backend_contract.md`](./00_shared_backend_contract.md).

**Thesis**: WireGuard is **not** a `MsgTransport`. It is an
interface-layer tunnel that is transparent to `socket(2)`, so
the correct abstraction is a *bindspace* — a scoped,
`@acm`-managed network context that an existing L4 transport
(`tcp`, and later `quic`/`tipc`-over-UDP-bearer) binds *inside*.
This plan implements `Address.namespace` (spec'd but unused
since day one) and the composed/tunnelled maddr grammar, with
`pyroute2` as the netlink codec and as much of the I/O moved
onto `trio` as the library's sans-io layer allows.

[#482]: https://github.com/goodboy/tractor/issues/482
[#443]: https://github.com/goodboy/tractor/issues/443

---

## 1. What exists today (verified, per #482)

- `wrap_address()` accepts maddr `str`s (leading-`/` dispatch,
  `_addr.py:262`) but `parse_maddr()` only knows
  `/ip4|ip6/<h>/tcp/<p>` and `/unix/<p>`; a `.../wg/u<key>`
  maddr raises `ValueError('Unsupported multiaddr protocol
  combo')`.
- there is no `wg` proto in the multiaddr spec; the first-draft
  upstream PR is multiformats/py-multiaddr#108 with key form
  `u<base64url>` (commit `8be3a8b`), tracked by
  multiformats/py-multiaddr#107 and gh #483.
- so **today's deployable story is declarative**: run `wg-quick`
  out-of-band, parse the maddr, strip to the inner
  `(host, port)`, verify the pubkey against the live tunnel,
  hand the inner addr to `registry_addrs=`/`tpt_bind_addrs=`.
  #482 already contains working example code for exactly this.
- `Address.namespace` exists in the Protocol
  (`_addr.py:94-101`, "the if-available OS-specific network
  namespace key") and **no backend implements it**. This plan is
  its first consumer.

## 2. Three layers, three PRs

| layer | what | dep | ships |
| --- | --- | --- | --- |
| **A. declarative** | commit #482's examples; `parse_maddr()` learns `/wg/u<key>` → inner `Address` + verified pubkey | `multiaddr` (already), `wg(8)` CLI | first |
| **B. `pyroute2` read/verify** | replace the `subprocess.run(['sudo','wg','show'])` shelling with netlink queries | `pyroute2` extra | second |
| **C. `@acm` lifecycle** | create/configure/tear down wg ifaces + netns *from the runtime*, as nested bindspaces; implement `Address.namespace` | `pyroute2` + `CAP_NET_ADMIN` | third |

Each is independently valuable and independently reviewable.
**Do not attempt C first** — the interesting design (nested
bindspace `@acm`s) is only well-posed once A has pinned the
address grammar and B has proven the netlink path under trio.

---

## 3. Layer A — declarative `wg` maddrs

### 3.1 the address shape

The decision: **a wg segment annotates an existing address, it
does not create a new address type.** Two candidate encodings;
**pick (a)**:

- **(a) `TunnelledAddress` wrapper** (recommended):
  ```python
  class TunnelledAddress(
      msgspec.Struct,
      frozen=True,
  ):
      inner: Address           # e.g. TCPAddress
      tunnel: WGTunnelSpec     # proto-specific, frozen
  ```
  with `.proto_key` **delegating to `inner.proto_key`** so every
  existing table lookup (`_addr_to_transport`,
  `enable_transports` guard at `_root.py:391`,
  `transport_from_addr()`) keeps working untouched, and
  `.unwrap()` delegating to `inner.unwrap()` so **nothing new
  crosses the wire**. `.namespace` and `.bindspace` come from
  the tunnel spec. The wrapper is stripped (`→ .inner`) at the
  moment of bind/connect.
  - ⚠️ `is_wrapped_addr()` (`_addr.py:194`) tests
    `type(addr) in _address_types.values()` — a `bidict` of
    proto_key→type. `TunnelledAddress` isn't in it and must not
    be (it's not 1:1 with a proto). So either add an explicit
    `isinstance(addr, TunnelledAddress)` clause there, or give
    the wrapper a marker and test structurally. Do the former;
    it's two lines and honest.
  - the reflection in `Endpoint.start_listener()`
    (`inspect.getmodule(self.addr)`) would resolve to the
    *wrapper's* module, not the transport's. **So the wrapper
    must be unwrapped before it reaches `Endpoint`** — i.e. by
    the bindspace `@acm` (layer C) or by `parse_maddr()`
    (layer A). State this loudly in the docstring; it's the #1
    way to get this wrong.
- (b) add fields to each existing `Address` type. Rejected:
  duplicates tunnel logic per-backend and pollutes `.unwrap()`.

```python
class WGTunnelSpec(
    msgspec.Struct,
    frozen=True,
):
    peer_pubkey: str          # std-base64 `wg(8)` form
    iface: str = 'wg0'
    netns: str|None = None
    # layer-C-only fields, unset in layer A
    maybe_endpoint: tuple[str, int]|None = None
    maybe_allowed_ips: tuple[str, ...] = ()
```

### 3.2 `parse_maddr()`/`mk_maddr()`

Grammar — **verified** against py-multiaddr#108
(`baudco/py-multiaddr@wg_support`, installed in a throwaway venv;
all three forms below parse *and* round-trip):

```
/ip4/192.168.1.50/udp/51820/wg/u<A_pub>/ip4/10.0.11.1/tcp/1616
\_______ bearer __________/\__ key __/\______ overlay ______/
 underlay, wg `ListenPort`             the ONLY part we bind
```

The `/wg/` segment is **infix, not suffix** — the segments
*before* it are the wg **bearer** (the underlay `(ip, udp-port)`
that `wg(8)` itself listens on, per the codec docstring's own
`/ip4/1.2.3.4/udp/51820/wg/{key}` example), and the segments
*after* are the **overlay** endpoint that `tractor` binds.

⚠️ **CORRECTION** — an earlier revision of this plan (and the
examples in gh #482) used a *suffix* form
`/ip4/10.0.11.1/tcp/1616/wg/u<key>`. That parses, but it is
semantically inverted: it puts the overlay addr where the bearer
belongs, `tcp` where wg's `udp` `ListenPort` goes, and declares
no overlay endpoint at all. `parse_wg_maddr()` in
`examples/wg_lan/` now rejects it with an actionable error.
Observed protocol-name lists, for writing the `match`:

| maddr | `[p.name for p in m.protocols()]` |
| --- | --- |
| `/ip4/1.2.3.4/udp/51820/wg/u<k>` | `['ip4','udp','wg']` |
| `/ip4/../udp/../wg/u<k>/ip4/../tcp/..` | `['ip4','udp','wg','ip4','tcp']` |

- so the three parts have **three different owners**, and only the
  third is an `Endpoint`:

  | part | bound by | in the runtime? |
  | --- | --- | --- |
  | bearer | kernel, via `wg-quick`/`pyroute2` | no |
  | `/wg/u<key>` | nothing — it's an identity | no, verified out-of-band |
  | overlay | `tractor`'s `IPCServer` | **yes**, as `.inner` |

  This owner-split is the real axis of the design, *not* whether
  the maddr stack is "composed" (it is).
- `parse_maddr()` gains a case on
  `[('ip4'|'ip6'), 'udp', 'wg', ('ip4'|'ip6'), <inner-l4>]` →
  build the inner `Address` from the trailing segments, decode
  the multibase key to std-base64, and return
  `TunnelledAddress(inner=..., tunnel=WGTunnelSpec(...))` with
  the bearer recorded in the spec.
- keep the existing 2-proto cases byte-identical; add the new
  case *after* them.
- generalize by **peeling at the tunnel segment**: split
  `proto_names` at `'wg'`, hand the trailing list to the existing
  inner-stack logic, and recurse for nested tunnels. Write it as
  a small pure fn `_peel_tunnel_segs(proto_names) ->
  (bearer_names, tunnel_specs, inner_names)`. This is also what
  makes a wg-inside-wg stack fall out for free.
- `mk_maddr()` inverse for `TunnelledAddress`.
- **blocked on upstream**: `Multiaddr('/…/wg/u…')` only parses
  once py-multiaddr#108 lands. Until then: pin the branch in the
  `wg` extra / dev-group and gate the tests on
  `_have_wg_maddr_proto()` (a cheap try/except around
  `Multiaddr('/wg/uAAAA')`). Do **not** hand-roll a `wg` parser
  in `tractor` — the whole point of #429 was dropping the NIH
  parser.

### 3.3 verification helper (pure, composable)

Port #482 §2's helpers into `tractor/discovery/_tunnel.py` as
*pure functions* + one impure probe, cleanly separated:

```python
def parse_wg_maddr(maddr: str) -> TunnelledAddress: ...   # pure
def wg8_pubkey(multibase_key: str) -> str: ...            # pure
def verify_wg_peer(spec: WGTunnelSpec) -> bool: ...       # impure probe
```

In layer A `verify_wg_peer()` may shell out (`wg show <if>
peers`), but it must be a *single* function so layer B swaps
only its body. Never call it implicitly from
`wrap_address()`/`parse_maddr()` — parsing must stay pure and
side-effect-free; verification is the *caller's* explicit step
(and later, the bindspace `@acm`'s).

### 3.4 deliverables

- `examples/` scripts distilled from #482 §§3-5 (this is the
  unchecked "commit examples from ^" bullet in #443).
- a `docs/` page: tunnel setup, the maddr form, the two-host
  run. Keep prose in the docs; keep the examples runnable and
  minimal.
- tests: maddr round-trip, `TunnelledAddress` delegation
  (`proto_key`/`unwrap` identical to inner), `wrap_address()`
  regression (a tunnelled maddr `str` → `TunnelledAddress`; a
  plain one → unchanged), and **a real end-to-end over a
  locally-created wg pair** gated on `CAP_NET_ADMIN` (see §5.3).

---

## 4. Layer B — `pyroute2` under `trio`

### 4.1 the library situation (verify at implementation time)

`pyroute2` ≥0.9 rewrote its core onto **asyncio**
(`AsyncIPRoute`; the sync `IPRoute` wraps it with its own loop).
It also ships a `WireGuard` netlink (generic-netlink) module
supporting `.set(iface, private_key=..., peer={...})` and
`.info(iface)`, plus `pyroute2.netns` / `NetNS` for namespaces,
and `IPRoute.link('add', kind='wireguard', ifname=...)`.

Three integration options, in increasing trio-nativeness:

- **(1) `trio.to_thread.run_sync()` around the sync API.**
  Netlink ops here are one-shot, sub-millisecond, and happen at
  bind/teardown time only — *not* in the msg hot path. This is
  the **correct default**: it's ~10 lines, uses a battle-tested
  API, and costs nothing where it's used.
- **(2) sans-io: `trio.socket` + pyroute2's message codecs.**
  `pyroute2`'s message classes
  (`pyroute2.netlink.rtnl.*`, `pyroute2.netlink.generic.wireguard.wgmsg`)
  encode/decode independently of its I/O core. So a
  `tractor/ipc/_netlink.py` with a small trio `NetlinkSocket`
  (`trio.socket.socket(AF_NETLINK, SOCK_RAW|SOCK_DGRAM, proto)`,
  `sendto`/`recv`, seq/pid matching, `NLMSG_DONE`/`NLMSG_ERROR`
  handling) + pyroute2 codecs is very achievable and is the
  honest reading of "as much trio wrapping as possible where any
  other async support can be replaced".
  **Do this for the paths we actually need** (link add/del,
  addr add, wg get/set, netns bind) and *only* those — a
  general netlink client is out of scope.
- (3) reimplement the codecs. Never.

**Recommended split**: ship (1) first so layer B is a small,
reviewable, behaviour-preserving swap of `verify_wg_peer()`'s
body; then land (2) as a follow-up commit for the read path
(`wg get`, `link get`) where the sans-io surface is smallest,
and keep (1) for the privileged mutating ops. Measure before
converting anything else — there is no perf argument here, only
a "no foreign event loop in a trio actor" argument, which (1)
already satisfies (a thread is not an event loop).

Explicitly **do not** pull in `trio-asyncio` for pyroute2: it
would be the one place in the runtime where an asyncio loop
exists for no reason.

### 4.2 API shape

Pure-ish, functional, `@acm` for anything with teardown:

```python
async def read_wg_peers(
    iface: str = 'wg0',
    netns: str|None = None,
) -> tuple[str, ...]: ...          # base64 pubkeys

async def read_wg_pubkey(iface: str = 'wg0', ...) -> str: ...
```

and `verify_wg_peer()` becomes a thin composition over the two.
Note the pure-getter rule: no `read_wg_peers(..., create=True)`.

---

## 5. Layer C — nested bindspace `@acm`s + `Address.namespace`

This is the part #443 and `multiaddr_declare_eps.md` actually
ask for: *"for any tunneled maddr-`str`-entry we deliver a
data-structure which can easily be passed to nested `@acm`s
which consecutively setup nested net bindspaces for binding the
endpoint addrs"*.

### 5.1 the composition

```python
@acm
async def open_bindspace(
    addr: TunnelledAddress,
) -> AsyncGenerator[Address, None]:
    '''
    Enter the net-bindspace implied by `addr`'s tunnel stack,
    yielding the *inner* `Address` ready to bind/connect.

    Nests: one `@acm` per tunnel segment, outermost-first, so
    a 2-deep stack is just two nested `async with`s and the
    teardown order is guaranteed by `trio`.

    '''
```

with per-tunnel-kind implementations:

```python
@acm
async def open_netns(name: str) -> AsyncGenerator[None, None]: ...
@acm
async def open_wg_iface(spec: WGTunnelSpec) -> AsyncGenerator[WGTunnelSpec, None]: ...
```

and a driver that folds a list of specs into nested contexts
(`contextlib.AsyncExitStack` for the N-deep case). The
`parse_endpoints()` API (`_multiaddr.py:153`) is the front door:
it already returns `dict[name, list[Address]]` and the
`multiaddr_declare_eps.md` sketch anticipates the recursive
`dict[str, list[Address]]|dict[...]` return for tunnelled
entries. Extend it to carry the tunnel stack, not to *enter* it.

### 5.2 `Address.namespace`, at last

- `TunnelledAddress.namespace` → `(kind, id)` e.g.
  `('netns', 'tractor-wg0')`.
- **and** the existing backends should implement it as `None`
  explicitly (they currently just don't define it), so the
  Protocol stops lying.
- consumers to audit: nothing reads `.namespace` today — so
  adding it is safe, but the *point* is that
  `Endpoint`/`Server.pformat()` should start showing it (there's
  already a `# !TODO, always be ns aware!` +
  `f'|_netns: {netns}\n'` placeholder sitting in
  `Endpoint.pformat()`, `_server.py:645`). Fill that in; it's
  the cheapest possible proof the layer is wired.

### 5.3 the netns/process reality — read this before designing

**The headline consequence, stated up front**: netns is a
**runtime-level config API, not an actor-app-code API.** It is
declared as part of how an actor process is *brought up* — a
spawn-time/boot-time input alongside `enable_transports` and
`tpt_bind_addrs` — and it is **not** dynamically re-enterable by
app code once the actor is live. There is deliberately no
`await actor.enter_netns(...)`. Two hard reasons, both below:
`setns(2)` doesn't retroactively move existing sockets, and it's
per-thread rather than per-process. Anything that *looks* like a
mid-life API here would be a footgun that silently leaves the IPC
server bound in the old namespace.

- `setns(2)` with `CLONE_NEWNET` affects **the calling thread
  only**, and sockets already created keep their original netns.
  A trio actor is effectively single-threaded for our purposes,
  so "enter the netns, *then* bind" works — but any
  `to_thread` worker (§4.1 option 1!) is in the **original**
  netns unless it also `setns`. Concretely: a wg query issued
  via `trio.to_thread` will hit the wrong namespace. Either
  pass `netns=` down to `pyroute2` (which does the
  fork/setns dance itself) or pin a dedicated worker. **This is
  the single subtlest bug in this plan — write the test first.**
- entering a netns is *process-global-ish and irreversible-ish*
  in practice. Therefore: **netns membership belongs to the
  actor process, decided before the runtime binds**, not to a
  mid-life `@acm`. Design:
  - the root/parent decides the netns for a subactor and passes
    it in the spawn spec (there's already
    `enable_transports`/`accept_addrs` plumbing at
    `_runtime.py:1595-1615` — the netns rides alongside).
  - the child, in `_runtime.async_main()` **before**
    `IPCServer.listen_on()`, enters it.
  - the mid-life `@acm` form is then only for the *root* /
    single-actor case, and for iface creation (which is
    genuinely scoped).
  - document the constraint rather than hiding it; a
    `RuntimeError` if `open_netns()` is entered after any
    listener exists.
- privileges: iface/netns creation needs `CAP_NET_ADMIN`.
  Never `sudo` from inside the runtime. Two supported modes:
  (i) pre-provisioned out-of-band (layers A/B — the default,
  and what #482 documents), (ii) runtime-managed when the
  process already holds the cap. Detect with a cheap
  `os.geteuid()==0 or CAP_NET_ADMIN in /proc/self/status`
  probe and *fail loudly with an actionable message* otherwise.
- teardown must be idempotent and tolerant: an iface/netns
  already gone must not strand the rest of the teardown — the
  exact lesson `_uds.close_listener()`'s `FileNotFoundError`
  tolerance and `_serve_ipc_eps()`'s per-ep `try/except`
  encode. Mirror both.

### 5.4 tests for layer C

- unit: fold-N-tunnel-specs-into-nested-`@acm`s, with fakes; assert
  enter/exit ordering (outermost-last-out) via a trace list.
- integration, gated on `CAP_NET_ADMIN` (skip otherwise, and in
  CI run it in a `--cap-add NET_ADMIN` container job): create two
  netns + a wg pair entirely in-process, boot a `tractor` root in
  one and a subactor in the other, `find_actor()` across the
  tunnel. This is a *fantastic* test to have and is fully
  self-contained — no second host, no `sudo` in the test body.
- the `to_thread`-netns-mismatch regression from §5.3, written
  **first** (red), then the fix (green), per project convention.

---

## 6. "Other shuttle-able tpts"

The generalization the #482 follow-up gestures at: once
`TunnelledAddress` + `open_bindspace()` exist, the same
machinery covers any iface-layer tunnel `pyroute2` can drive —
`ipip`/`gre`/`sit`/`vxlan`/`geneve`/`bridge`/`veth`. Keep
`WGTunnelSpec` as *one* frozen struct among a
`TunnelSpec = WGTunnelSpec|VxlanTunnelSpec|...` union with a
`kind: ClassVar[str]`, and dispatch `open_*` by `match` on it.
Design for it now (union + `match`), implement only `wg` +
`netns`. `veth`-pairs-in-netns is the natural second one because
it makes the §5.4 integration test possible without wg at all —
consider doing it *first* for exactly that reason.

## 7. Non-goals

- no wg userspace implementation, no key exchange, no
  `wg-quick` reimplementation (config-file parsing is
  out of scope; take structured input).
- no persistence of private keys beyond what layer C's iface
  creation needs (and that stays in `get_rt_dir()`, 0600).
- macOS/Windows: layers B/C are Linux-only. Layer A (declarative)
  works anywhere `wg` does. Gate accordingly and say so in the
  docs — do not silently no-op.

## 8. Risks

| risk | mitigation |
| --- | --- |
| `to_thread` worker runs in the wrong netns | §5.3; pass `netns=` to pyroute2 or pin a worker; test-first |
| py-multiaddr#108 not merged | branch pin + `_have_wg_maddr_proto()` gate; layer A's inner-addr path works regardless |
| `TunnelledAddress` leaks into `Endpoint` and breaks `inspect.getmodule()` | unwrap at parse/bindspace boundary; assert `not isinstance(ep.addr, TunnelledAddress)` in `Endpoint.__post_init__` |
| privileged ops in a library | never `sudo`; explicit cap probe + actionable error; pre-provisioned is the default |
| pyroute2 0.9 asyncio core drags a loop into the actor | option (1) is a *thread*, not a loop; forbid `trio-asyncio` here (§4.1) |
| netns teardown strands actor teardown | idempotent/tolerant teardown mirroring `_uds.close_listener()` |

## 9. Follow-up issue seeds

- `veth`-in-netns bindspace (unblocks capless-ish integration
  testing, and is a great local multi-"host" test rig)
- composed/tunnelled maddr grammar shared with plan 02's
  `/…/quic-v1/…` stacks (gh #443)
- `wg` proto into the multiaddr **spec** (gh #483), then flip
  `MsgTransport.maddr` to always return `Multiaddr` (the third
  #443 bullet)
- runtime-managed wg key rotation / peer add-remove as a
  `tractor` service actor — the natural "actor that owns the
  network" demo
