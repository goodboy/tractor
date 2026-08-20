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
  `_addr.py:262`). `parse_maddr()` and `mk_maddr()` support plain
  TCP/UDS addresses plus nested, canonical bearer-first `/wg/`
  stacks represented locally as `TunnelledAddress` wrappers.
- there is no `wg` proto in the multiaddr *spec* yet, but
  multiformats/py-multiaddr#108 (key form `u<base64url>`) is
  **merged** as of 2026-07-28 (`f86519da`) — and unreleased, the
  latest `0.2.0` predating it. Spec registration is still tracked
  by multiformats/py-multiaddr#107 and gh #483.
- **today's deployable story remains declarative**: run `wg-quick`
  out-of-band, parse the maddr, strip its wrapper to the overlay
  `(host, port)`, verify the pubkey against the live tunnel,
  hand the overlay addr to `registry_addrs=`/`tpt_bind_addrs=`.
  #482 already contains working example code for exactly this.
- `Address.namespace` exists in the Protocol
  (`_addr.py:94-101`, "the if-available OS-specific network
  namespace key"). `TunnelledAddress` implements it from its spec;
  no concrete transport backend implements it yet.

## 2. Three layers, three PRs

| layer | what | dep | ships |
| --- | --- | --- | --- |
| **A. declarative** | commit #482's examples; `parse_maddr()` learns `/wg/u<key>` → overlay `Address` + verified pubkey | `multiaddr` (already), `wg(8)` CLI | first |
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
      overlay: Address         # e.g. TCPAddress
      tunnel: WGTunnelSpec     # proto-specific, frozen
  ```
  with `.proto_key` **delegating to `overlay.proto_key`** so every
  existing table lookup (`_addr_to_transport`,
  `enable_transports` guard at `_root.py:391`,
  `transport_from_addr()`) keeps working untouched, and
  `.unwrap()` delegating to `overlay.unwrap()` so **nothing new
  crosses the wire**. `.namespace` and `.bindspace` come from
  the tunnel spec. The wrapper is stripped (`→ .overlay`) at the
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

Grammar — **verified** against py-multiaddr#108, first on the
`baudco/py-multiaddr@wg_support` branch and re-verified after it
merged upstream (`multiformats/py-multiaddr@f86519da`); all three
forms below parse *and* round-trip. Note the codec also validates
that the key decodes to exactly 32 bytes, so a truncated key is a
`StringParseError`, not a silently-mangled parse:

```
/ip4/192.168.1.50/udp/51820/wg/u<A_pub>/ip4/10.0.11.1/tcp/1616
\_______ bearer __________/\__ key __/\______ overlay ______/
 underlay, wg `ListenPort`             the `MsgTransport` bind
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
`examples/multihost/wg_lan/` now rejects it with an actionable
error.
Observed protocol-name lists, for writing the `match`:

| maddr | `[p.name for p in m.protocols()]` |
| --- | --- |
| `/ip4/1.2.3.4/udp/51820/wg/u<k>` | `['ip4','udp','wg']` |
| `/ip4/../udp/../wg/u<k>/ip4/../tcp/..` | `['ip4','udp','wg','ip4','tcp']` |

- so the three parts have **three different owners**, and only the
  third is an `Endpoint`:

  | part | socket owner / provisioner | runtime role |
  | --- | --- | --- |
  | bearer | kernel-owned; externally provisioned in layer A, tractor bindspace-provisioned in layer C | control-plane metadata, never an `Endpoint` |
  | `/wg/u<key>` | nothing — it's an identity | parsed and explicitly verified |
  | overlay | `tractor`'s `IPCServer` | application `MsgTransport`, as `.overlay` |

  This owner-split is the real axis of the design, *not* whether
  the maddr stack is "composed" (it is).
- ⚠️ **CORRECTION**, an earlier draft of this section specced a
  hand-rolled `_peel_tunnel_segs(proto_names) -> (bearer_names,
  tunnel_specs, overlay_names)`. **Do not write it.**
  `py-multiaddr` already ships the whole tunnel compose/peel API
  and it was simply missed here — see its README "En/decapsulate"
  and "Tunneling" sections, and gh #443's 2nd bullet which links
  them. Verified against the pinned rev:

  | need | API |
  | --- | --- |
  | isolate the bearer | `ma.decapsulate_code(P_WG)` |
  | drop the overlay, keep bearer+key | `ma.decapsulate(overlay_ma)` |
  | per-seg maddrs | `ma.split()` |
  | rejoin a seg tail | `Multiaddr.join(*segs)` |
  | read the key | `ma.value_for_protocol('wg')` |
  | recompose | `bearer.encapsulate(key).encapsulate(overlay)` |

  `.decapsulate_code()` handles the infix `/wg/` seg cleanly
  *because* it cuts on proto-code and never tries to match an
  addr value — the key seg has no addr of its own. This is the
  same NIH trap gh #429 existed to close, one layer up.

- ⚠️ `value_for_protocol('ip4')` on a *full* tunnelled maddr
  silently returns the **first** match, i.e. the bearer's host.
  Always call it on a peeled sub-maddr, never the whole stack.

- `parse_maddr()` gains a case on
  `[('ip4'|'ip6'), 'udp', 'wg', ('ip4'|'ip6'), <overlay-l4>]` →
  peel w/ the API above, decode the multibase key to std-base64,
  and return `TunnelledAddress(overlay=..., tunnel=WGTunnelSpec(
  ...))` w/ the bearer recorded in the spec.
- keep the existing 2-proto cases byte-identical; add the new
  case *after* them.
- nesting (wg-in-wg) falls out of `.decapsulate_code()` cutting
  at the *last* occurrence — peel repeatedly rather than
  recursing through a bespoke splitter.
- `mk_maddr()` inverse for `TunnelledAddress` is just
  `.encapsulate()` composition; don't rebuild `str`s by hand.
- **pending an upstream release**: py-multiaddr#108 is merged, so
  `Multiaddr('/…/wg/u…')` parses off a PEP 621 direct-revision pin,
  since no release carries the codec. Gate parser entry on
  `_wg_proto_code()`, implemented as
  `protocols.protocol_with_name('wg')` under
  `except ProtocolNotFoundError`. Do **not** probe by parsing a
  dummy like `Multiaddr('/wg/uAAAA')` — the codec enforces a
  32-byte key, so that raises even when the proto *is* known. Do
  **not** hand-roll a `wg` parser in `tractor` — the whole point
  of #429 was dropping the NIH parser.

### 3.3 verification helper (pure, composable)

Port #482 §2's pure helpers into
`tractor/discovery/_tunnel.py`, keeping the impure probe cleanly
separated until layer B:

```python
def parse_wg_maddr(maddr: str) -> TunnelledAddress: ...   # pure
def wg8_pubkey(multibase_key: str) -> str: ...            # pure
def verify_wg_peer(spec: WGTunnelSpec) -> bool: ...       # layer B
```

In layer A `verify_wg_peer()` may shell out (`wg show <if>
peers`), but it must be a *single* function so layer B swaps
only its body. Never call it implicitly from
`wrap_address()`/`parse_maddr()` — parsing must stay pure and
side-effect-free; verification is the *caller's* explicit step
(and later, the bindspace `@acm`'s).

### 3.4 deliverables

- `examples/` scripts distilled from #482 §§3-5 (this is the
  unchecked "commit examples from ^" bullet in #443). They live
  under `examples/multihost/` — `test_docs_examples.py` walks
  `examples/` recursively and runs every collected file as a
  subproc asserting `rc == 0` (it doesn't even filter by
  extension, so a stray `README.md` would be `python`-run too),
  and `'multihost' not in p[0]` is already in its exclusion
  list. Anything needing a real second host or a live tunnel
  belongs there.
- a `docs/` page: tunnel setup, the maddr form, the two-host
  run. Keep prose in the docs; keep the examples runnable and
  minimal.
- tests: maddr round-trip, `TunnelledAddress` delegation
  (`proto_key`/`unwrap` identical to overlay), `wrap_address()`
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

Layer C is where tractor takes ownership of bindspace orchestration.
For a fully bootstrapped deployment it may create the netns and wg
iface, configure peers/routes, and ask the kernel to establish the
bearer's UDP `ListenPort` through netlink/`pyroute2`. "Kernel-owned"
describes the data-plane socket, not who provisions it: tractor owns
the lifecycle while `Endpoint`/`MsgTransport` remain responsible only
for the overlay application socket.

### 5.1 the composition

The maddr describes the composed network path and can be used as
either a source/listen or destination/dial handle. It does **not**
select the local instance of that network stack. A netns, VRF,
interface, user namespace, or equivalent platform resource is
orthogonal augmentation carried alongside/below the maddr.

Keep two bindspace representations with deliberately different
lifetimes:

```python
class BindspaceSpec(msgspec.Struct, frozen=True):
    '''Serializable spawn/config declaration.'''
    kind: str                 # `netns`, later `vrf`, ...
    key: str|None             # requested name/key, if any


class BindspaceIdentity(msgspec.Struct, frozen=True):
    '''Stable identity of the realized platform resource.'''
    kind: str
    key: str|None
    inode: int|None           # Linux namespace identity


class BindspaceHandle:
    '''Scoped, non-serializable capability for one live bindspace.'''
    spec: BindspaceSpec
    identity: BindspaceIdentity
    namespace_fd: int|None
    ownership: Literal['owned', 'borrowed']


@acm
async def open_bindspace(
    spec: BindspaceSpec,
    *,
    role: Literal['listen', 'dial'],
) -> AsyncGenerator[BindspaceHandle, None]:
    '''
    Provision/borrow one bindspace and yield its live capability.

    '''
```

The exact field set remains design work; the required split does not:
`BindspaceSpec` crosses config/spawn serialization, while
`BindspaceHandle` contains live OS resources (especially an open
namespace FD), pins identity/lifetime, and must never cross msgpack.
An FD is a stronger capability than a namespace name: it avoids
name-resolution TOCTOU, survives rename/unlink, and identifies the
exact namespace the parent provisioned.

`open_bindspace()` is **not** an address factory and does not return a
`TunnelledAddress`. At the declaration layer, listener allocation can
use the handle to replace an overlay while preserving every tunnel:

```python
async with open_bindspace(
    bindspace_spec,
    role='listen',
) as bindspace:
    listen_decl = declared_addr.get_random(
        bindspace=bindspace,
    )
    transport_addr = strip_tunnels(listen_decl)
```

That sketch intentionally leaves the `.get_random()`/bindspace value
contract open. A concrete transport call returns a concrete overlay;
a declaration-level call may replace the overlay and return a new
`TunnelledAddress`. In either case wrappers remain until the final
transport bind/dial boundary, where `strip_tunnels()` is mandatory.

Per-platform provisioning still composes one resource context per
tunnel/bindspace layer:

```python
@acm
async def open_netns(
    spec: BindspaceSpec,
    role: Literal['listen', 'dial'],
) -> AsyncGenerator[BindspaceHandle, None]: ...

@acm
async def open_wg_iface(
    spec: WGTunnelSpec,
    bindspace: BindspaceHandle,
    role: Literal['listen', 'dial'],
) -> AsyncGenerator[WGTunnelSpec, None]: ...
```

and a driver that folds a list of specs into nested contexts
(`contextlib.AsyncExitStack` for the N-deep case). The
`parse_endpoints()` API (`_multiaddr.py:153`) is the front door:
it already returns
`dict[name, list[Address|TunnelledAddress]]` and the
`multiaddr_declare_eps.md` sketch anticipates the recursive
`dict[str, list[Address]]|dict[...]` return for tunnelled
entries. Extend it to carry the tunnel stack, not to *enter* it.

The caller supplies `role`; do not infer it from maddr shape. The same
composed maddr can name a server source or client destination, and the
required local provisioning/ownership differs (§5.3).

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

Use `github/ns_aware@e4688cad` as prototype evidence, not code to
cherry-pick unchanged. Its `/proc/<pid>/ns/<type>` inode reader and
`ip netns identify` probe establish the useful `(key, inode)` identity
pair. Layer C should move that shape into `BindspaceIdentity`, avoid a
subprocess where netlink/procfs suffices, and hold the namespace FD in
`BindspaceHandle` to pin the identity.

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
  mid-life actor API. Design:
  - the root/parent decides the `BindspaceSpec`, provisions or
    borrows it, and passes the spec plus an inherited/transferred
    namespace-FD capability through the spawn backend (there's already
    `enable_transports`/`accept_addrs` plumbing at
    `_runtime.py:1595-1615` — the netns rides alongside).
  - the child spawn/bootstrap trampoline calls `setns()` **before**
    `_runtime.async_main()`, `IPCServer.listen_on()`, parent-channel
    connection, or creation of any worker thread/socket.
  - only after successful entry does the child drop namespace-entry
    privileges and initialize the actor runtime.
  - a root/single-actor process follows the same ordering: enter during
    root bootstrap, never after actor runtime startup.
  - iface/route/WG provisioning is genuinely scoped and remains under
    the parent/supervisor's `BindspaceHandle` context.
  - document the constraint rather than hiding it; a
    `RuntimeError` if namespace entry is attempted after bootstrap.
- capabilities: iface/netns creation/config needs `CAP_NET_ADMIN`;
  entering an existing Linux namespace normally requires
  `CAP_SYS_ADMIN` in the owning user namespace. Never `sudo` from
  inside the runtime. A privileged parent/helper should provision the
  stack and open the namespace FD; the child receives only the scoped
  capability and temporary authority needed to enter it, then drops
  that authority before actor code runs. This separates create/config
  authority from enter/use authority and fits user-namespace/capability
  deployments without granting every actor broad ambient caps.
  Two supported modes remain:
  (i) pre-provisioned out-of-band (layers A/B — the default,
  and what #482 documents), (ii) runtime-managed when the supervising
  process/helper holds the required caps. Probe exact required caps and
  *fail loudly with an actionable message* otherwise.
- role semantics are explicit:
  - `listen`: may create/own the local bindspace, iface, routes, WG
    peer/listener state, and random local overlay; lifetime normally
    extends through all listeners and the actor process.
  - `dial`: may borrow an actor-wide bindspace or ensure local routing
    and tunnel state reaches the remote stack; it does not own the
    remote maddr and may need no new local resource at all.
  - source/destination use is an operation property, never permanently
    encoded into the maddr or inferred from segment ordering.
- teardown follows capability ownership, not just address type:
  - owned listener bindspaces tear down after endpoints/channels and
    the actor process have exited;
  - borrowed dial/actor-wide bindspaces only release their handle;
  - nested resources exit inside-out, but shared resources remain until
    their owning supervisor drops the final capability.
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
- bootstrap ordering: assert the child reports the expected namespace
  inode before parent-channel connect and listener creation.
- FD capability: rename/unlink the namespace name after opening its FD
  and prove child entry still selects the pinned inode.
- privilege drop: prove actor code lacks provisioning caps after entry.
- role/ownership: fake listen/dial resources and assert owned listener
  teardown versus borrowed dial-handle release.

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
| namespace name is renamed/replaced between provision and spawn | pass an open namespace FD; verify `(key, inode)` after child entry |
| child starts sockets/threads before `setns()` | enter in the spawn bootstrap trampoline before `_runtime.async_main()`; assert inode ordering |
| ambient capabilities leak into actor app code | split provision/enter authority and drop caps before runtime initialization |
| dial path tears down a shared actor bindspace | encode ownership in `BindspaceHandle`; borrowed handles never remove resources |
| py-multiaddr#108 merged but unreleased | PEP 621 direct-revision pin + `_wg_proto_code()` gate; replace with a release floor once published |
| `TunnelledAddress` leaks into transport reflection/type dispatch | keep wrappers through declaration/bindspace handling, call `strip_tunnels()` at channel/endpoint boundaries, and retain the boundary regressions |
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
