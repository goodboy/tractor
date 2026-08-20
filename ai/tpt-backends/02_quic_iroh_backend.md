# Plan 02 — QUIC backend via `iroh` FFI, uniffi-async rewritten onto `trio`

Tracks gh [#353]. Prereq reading:
[`00_shared_backend_contract.md`](./00_shared_backend_contract.md).

**Thesis**: the value of `iroh` over "just QUIC" is
`NodeId`-addressed, NAT-traversing, relay-fallback endpoints —
i.e. a `tractor` actor tree that spans hosts *without* a
reachable listening socket. The cost is that `iroh`'s python
surface is `uniffi`-generated **asyncio** and its listener is not
a socket. This plan spends its complexity budget in exactly two
places: a `trio`-native uniffi future bridge, and a
`trio.abc.Listener`/`Stream` adapter pair. Everything else is
contract boilerplate.

[#353]: https://github.com/goodboy/tractor/issues/353

---

## 1. Library selection (decided, with the rejected alternatives)

**Chosen: `iroh` (PyPI, from `n0-computer/iroh-ffi`), pinned to
a single minor.** The `iroh` python package is a `uniffi`
binding over the rust `iroh` crate (QUIC via `quinn`/`noq`).

Rejected, and why — record these so the next implementer doesn't
relitigate:

- **`aioquic`** (sans-io + asyncio): genuinely trio-portable
  (`hypercorn` already pairs its sans-io core with a trio UDP
  server, see the links in #353) and dependency-light. But it
  gives us *only* QUIC — no NodeId identity, no hole punching,
  no relay. We'd be reimplementing iroh's whole reason for
  existing. **Keep as the documented fallback** if the FFI
  bridge (§2) proves unmaintainable; the `MsgTransport` and
  `Listener` adapters from §3 are ~90% reusable against an
  `aioquic` core, which is a deliberate design property of this
  plan.
- **`quiche` / `quinn` via a hand-rolled PyO3 ext**: strictly
  more work than reusing `iroh-ffi`, and puts us in the
  build-wheels business.
- **`trio-asyncio`**: viable *shortcut* to run the asyncio-shaped
  bindings under trio, and `tractor` already ships
  infected-asyncio machinery (`tractor.to_asyncio`,
  `tests/test_infected_asyncio.py`). Rejected as the *primary*
  design because it makes every IPC send/recv cross a
  loop-boundary shim in the hot path, and because #353 asks
  explicitly for the asyncio support to be "rewritten for trio".
  **But**: build it first as the throwaway spike (§6 step 0) to
  de-risk the iroh API surface before writing the bridge.

Version pinning: `iroh` moves fast and has had breaking
API renames across minors. Pin `iroh>=X.Y,<X.Y+1` in a `quic`
extra, and **write down the exact resolved version + the
generated `iroh/_uniffi*` module layout** in the module
docstring, because §2 depends on generated-code internals.

**Step 0 of implementation is an API-truth pass**: install the
pinned `iroh`, `python -c "import iroh; help(iroh)"`, and record
in this doc's §1.1 the real names of: endpoint builder, secret
key type, `connect`/`accept`, bi-stream open/accept, the
send/recv methods and their exact signatures/return types, and
whether they're `async def`. Everything below uses *provisional*
names and must be reconciled. Do not skip this; do not guess
from memory.

### 1.1 API-truth table (fill in during step 0)

| concept | provisional name | actual (fill in) |
| --- | --- | --- |
| secret key | `iroh.SecretKey.generate()` | |
| endpoint builder | `iroh.Endpoint.builder(...).bind()` | |
| node id | `endpoint.node_id() -> str` | |
| node addr (relay + direct) | `iroh.NodeAddr` | |
| dial | `await endpoint.connect(node_addr, alpn)` | |
| accept conn | `await endpoint.accept()` | |
| open bi-stream | `await conn.open_bi()` | |
| accept bi-stream | `await conn.accept_bi()` | |
| send | `await send_stream.write_all(b)` | |
| recv | `await recv_stream.read(n) -> bytes\|None` | |
| half-close | `await send_stream.finish()` | |

---

## 2. The `trio`-native uniffi future bridge (`tractor/ipc/_uniffi_trio.py`)

### 2.1 what uniffi actually generates

`uniffi`'s async support does not use asyncio *semantically* —
it uses asyncio only as the *executor* for a poll loop. The
generated python for an `async fn` is, in shape:

1. call `_uniffi_..._<method>(...)` → returns an opaque
   `RustFuture` handle (a `void*`/`u64`).
2. loop: call
   `ffi_..._rust_future_poll_<T>(handle, callback, callback_data)`.
   The callback is a C-ABI fn pointer invoked **from an
   arbitrary rust thread** with a poll-result code
   (`READY`/`MAYBE_READY`).
3. the generated glue's callback resolves an
   `asyncio.Future` via `loop.call_soon_threadsafe(...)`; the
   coroutine awaits it, then re-polls.
4. on ready: `ffi_..._rust_future_complete_<T>(handle,
   &call_status)` → the value; then
   `ffi_..._rust_future_free_<T>(handle)`.

**The asyncio dependency is confined to step 3.** That is the
whole insight: the bridge is ~40 lines.

### 2.2 the trio version

```python
async def await_rust_future(
    poll: Callable,      # ffi_..._rust_future_poll_<T>
    complete: Callable,  # ffi_..._rust_future_complete_<T>
    free: Callable,      # ffi_..._rust_future_free_<T>
    handle: int,
    lift: Callable[[Any], Any],
) -> Any:
    '''
    Drive a `uniffi` rust-future to completion on the current
    `trio` task, bridging rust-thread wakeups via
    `TrioToken.run_sync_soon()`.

    '''
    token = trio.lowlevel.current_trio_token()
    while True:
        wake = trio.Event()
        # NOTE, invoked from a *rust* thread!
        def _cb(_data, poll_code):
            token.run_sync_soon(wake.set)

        cb = _UNIFFI_FUTURE_CALLBACK(_cb)   # keep a strong ref!
        poll(handle, cb, 0)
        await wake.wait()
        if <poll_code was READY>:
            break
    try:
        status = _UniffiRustCallStatus.default()
        res = complete(handle, status)
        _uniffi_check_call_status(status)   # reuse generated helper
        return lift(res)
    finally:
        free(handle)
```

Critical details, each a real bug if missed:

- **`token.run_sync_soon()` is the only trio API callable from a
  foreign thread**, and it is documented as such. Use it; do
  *not* use `trio.from_thread.run_sync` (requires a trio thread
  context) and do not touch the `Event` directly from the
  callback.
- **the poll code must reach the trio side.** Capture it in a
  `nonlocal`/1-slot list written by the callback *before*
  `run_sync_soon`, since the callback owns the value. Handle
  `MAYBE_READY` by re-polling (the loop above does).
- **keep the `ctypes` callback object alive** across the await —
  a GC'd `CFUNCTYPE` trampoline is a segfault. Bind it to a
  local *and* make sure the local outlives the `poll()` call
  window.
- **cancellation.** `await wake.wait()` is a trio checkpoint, so
  a `Cancelled` can fire while rust still owns the future. On
  cancel we must still `free(handle)` — and per uniffi, the
  correct sequence is to call the generated
  `ffi_..._rust_future_cancel_<T>(handle)` then continue
  polling to completion before `free`. Wrap the whole thing so
  the cancel path does:
  `with trio.CancelScope(shield=True): cancel(handle); <drain
  poll loop>; free(handle)`. **Bounded** shield (add a
  `trio.move_on_after()` with a module-level constant) so a
  wedged rust future can't make an actor un-cancellable —
  `tractor` is SC-first and an unbounded shield here would
  violate that.
- **`trio.lowlevel.current_trio_token()`** must be captured on
  the trio side (not in the callback).

### 2.3 how to apply it to the generated bindings

Do **not** fork/vendor the generated `iroh` python. Instead ship
a *narrow* re-dispatch shim:

- write `tractor/ipc/_uniffi_trio.py` with `await_rust_future()`
  plus a `@cm patch_uniffi_for_trio()` that monkey-patches the
  generated module's single async-driver entrypoint (in current
  uniffi that's `_uniffi_rust_call_async` / `_rust_call_async`,
  one function) to the trio implementation.
- verify at import time that the expected symbol exists and
  raise a clear, actionable error naming the pinned `iroh`
  version if not. A silent fallback to asyncio would be a
  nightmare to debug.
- **plan for this to break on `iroh`/`uniffi` upgrades.** Mitigate
  with (a) a unit test that drives one trivial `iroh` async call
  under bare `trio.run()` and asserts no event loop was ever
  created (`asyncio.get_event_loop_policy()` untouched /
  `asyncio._get_running_loop() is None`), and (b) a docstring
  pointing at the uniffi codegen template this mirrors.

If step 0 reveals the generated code is *structurally* hostile
to this (e.g. `asyncio` imported and used at module scope for
more than the driver), fall back to option (b): run iroh under
`tractor.to_asyncio` infected mode and open the follow-up to
revisit. Say so in the PR rather than fighting it.

---

## 3. Mapping QUIC onto `MsgTransport`

### 3.1 the layering decision

QUIC natively multiplexes streams inside one connection. The
mapping that preserves *all* existing `tractor` semantics with
the least new code:

```
iroh Endpoint      ==  one per actor (process)     -> the "listener"
iroh Connection    ==  one per peer actor           -> pooled
iroh bi-stream     ==  one `Channel`/`MsgTransport`  -> 1:1
```

- keep the 4-byte `<I` length-prefix framing **unchanged**. It's
  redundant-ish over a QUIC stream but it means
  `MsgpackTransport` is reused verbatim, and framing is cheap.
  Revisit only after it works.
- **one-task-per-stream** falls out naturally, which is exactly
  the #353 note about QUIC sub-stream QoS/cancellation fitting
  `trio`.
- `layer_key: int = 4` still (QUIC is L4-ish); note in a comment
  that this backend is really 4+security+multiplex.

**Connection pooling** is the one place we add state the other
backends don't have: dialing the same peer twice should reuse
the `Connection` and open a second bi-stream. Implement as a
module-level `dict[NodeId, Connection]` guarded by a
`trio.Lock`... **no** — that's a per-process cache with
lifetime/teardown hazards. Instead reuse the codebase's existing
idiom: `tractor.trionics.maybe_open_context()` keyed on the
node-id, which already solves exactly this (one-cached-resource-
per-key, refcounted, teardown-on-last-exit) and whose teardown
semantics were just hardened (gh #488). Use it; do not hand-roll
a cache. Anything concurrency-subtle here should get the
`conc-anal` skill run over it.

### 3.2 `IrohAddress`

```python
class IrohAddress(
    msgspec.Struct,
    frozen=True,
):
    _node_id: str                 # 32B ed25519 pubkey, hex or z32
    _alpn: str = 'tractor/0'      # the bindspace!
    # optional dial hints; NOT part of identity
    maybe_relay_url: str|None = None
    maybe_direct_addrs: tuple[str, ...] = ()

    proto_key: ClassVar[str] = 'iroh'   # ?or 'quic'; see §3.2.1
    unwrapped_type: ClassVar[type] = tuple[str, str]
    def_bindspace: ClassVar[str] = 'tractor/0'
```

- **`.unwrap() -> (node_id_str, alpn_str)`** — a `(str, str)`
  tuple, which is *unambiguously distinct* from
  `TCPAddress`'s `(str, int)`. But careful:
  `wrap_address()`'s UDS case is
  `case (_, filename) if type(filename) is str` — which
  **already catches `(str, str)`**. So the iroh `case` MUST be
  ordered *before* the UDS case and guarded, e.g.
  `case (str() as nid, str() as alpn) if _is_node_id(nid):`
  with `_is_node_id()` a cheap length+alphabet check. Add a
  regression test asserting a UDS `(dir, filename)` pair still
  wraps to `UDSAddress` — this is the exact "wrong transport
  loaded" hazard `_addr.py:214` warns about.
- `.bindspace` → `self._alpn`. This is the honest analogue:
  the ALPN is the set of endpoints willing to talk to you, and
  two `tractor` deployments sharing an iroh network are
  separated by ALPN exactly as two UDS deployments are
  separated by directory. Include a `tractor` version/proto
  epoch in the default ALPN so incompatible runtimes can't
  handshake.
- `.is_valid` → node-id parses, alpn non-empty.
- **`get_root()` is the hard one.** There is no
  well-known-port analogue: an iroh node id is a *keypair*, so
  "the host's default registrar addr" requires a *persisted
  secret key*. Design:
  - the root/registrar's secret key lives at
    `get_rt_dir() / 'iroh_registrar.key'` (0600), created on
    first use.
  - `get_root()` must stay **pure and import-time-safe**
    (contract §2.3: `_default_lo_addrs` is built at import!).
    So `get_root()` *reads* the key file if present and
    otherwise returns an `IrohAddress` with
    `_node_id=''`/sentinel, and the **generation** happens in
    an explicit sibling — `ensure_registrar_key() ->
    IrohAddress` — called from the listen path. Pure getter,
    explicit setter; do not smuggle key generation into
    `get_root()`.
  - this almost certainly means `_default_lo_addrs` must become
    lazy for this backend. **Land that refactor as its own prep
    commit** (a `default_lo_addrs()` that computes per-call
    instead of the import-time dict) — it also unblocks plan
    03's netns-scoped defaults.
- `get_random()`: generate a fresh `SecretKey` per subactor and
  return its node-id. Note this runs post-fork pre-listen
  (contract §4) and costs an ed25519 keygen (~µs, fine). The
  *secret* can't live in a frozen `Address`, so it must be
  stashed where the listen path can find it: a module-level
  `dict[node_id, SecretKey]` populated by `get_random()` and
  consumed+popped by `start_listener()`. Ugly but honest;
  document it and note the alternative (thread the key through
  `Endpoint`) as a follow-up.

#### 3.2.1 `proto_key`: `'iroh'` vs `'quic'`

Use **`'quic'`** for the `proto_key`/`--tpt-proto` name and
name the module `_quic.py`, with `iroh` as the *implementation*.
Rationale: it keeps the door open for the `aioquic` fallback
(§1) without a user-visible rename, and it matches how `uds` is
a proto name rather than a lib name. Put `iroh`-specific bits
behind an internal `_iroh` submodule if the file gets big.

### 3.3 the `trio.abc` adapters — where the real work is

Contract §3 says a non-socket backend needs three upstream
generalizations. Land them **as a prep PR, before any iroh
code**, so they can be reviewed on their own merits with
tcp/uds still the only backends:

1. **`Endpoint.start_listener()` must not assume
   `.socket.getsockname()`.** Use the same
   `Address.rebind_from_sockname: ClassVar[bool]` gate that
   plan 01 §3.2 introduces — coordinate so it lands once. (If
   plan 01 lands first, this is free.)
2. **`transport_from_stream()` (`_types.py:92`) must not assume
   `trio.SocketStream`.** Replace the `sock.family` match with:
   check `isinstance(stream, trio.SocketStream)` → existing
   family match; else look for a
   `stream.tpt_key: ClassVar[MsgTransportKey]` attribute on the
   adapter and use it. Keeps the existing path byte-identical
   and makes new stream types self-describing (a much better
   shape than growing an `isinstance` ladder).
3. **type annotations**: `handle_stream_from_peer(stream:
   trio.SocketStream)` → `trio.abc.Stream`; `Endpoint._listener:
   SocketListener|None` → `trio.abc.Listener|None`;
   `MsgTransport.stream: trio.SocketStream` →
   `trio.abc.Stream`. Annotation-only, zero behaviour change.

Then the adapters:

```python
class QuicMsgStream(trio.abc.HalfCloseableStream):
    '''
    A single `iroh` bi-directional QUIC stream presented as
    a `trio` byte-stream so `MsgpackTransport` can frame over
    it unmodified.

    '''
    tpt_key: ClassVar[MsgTransportKey] = ('msgpack', 'quic')

    def __init__(self, conn, send, recv) -> None: ...
    async def send_all(self, data: bytes) -> None: ...
    async def wait_send_all_might_not_block(self) -> None: ...
    async def receive_some(self, max_bytes: int|None = None) -> bytes: ...
    async def send_eof(self) -> None: ...
    async def aclose(self) -> None: ...
```

Non-negotiable behaviours (each maps to a `match` case that
already exists in `_transport.py` and must keep working):

- `receive_some()` returns `b''` at clean EOF →
  `MsgpackTransport._iter_packets()` sees `header == b''` and
  raises `TransportClosed(loglevel='transport')`. **This is the
  graceful-disconnect path the whole runtime relies on**; get it
  right first.
- a reset/aborted stream → raise `trio.BrokenResourceError`.
- use after local close → raise `trio.ClosedResourceError`
  (ideally with `'another task closed this fd'`-equivalent text
  absent, so the `raise_on_report` branch at
  `_transport.py:290` stays quiet).
- `send_all()` on a closed peer → `trio.BrokenResourceError`.
- honour `trio`'s one-task-per-direction rule: guard with
  `trio._util.ConflictDetector` equivalents (or just document +
  assert), because `MsgpackTransport` already serializes sends
  with a `StrictFIFOLock` but recvs are single-task by
  construction.
- **buffering**: if iroh's `read()` doesn't support
  "read up to n", `receive_some()` must maintain an internal
  leftover buffer. Note `MsgpackTransport` wraps us in
  `tricycle.BufferedReceiveStream` anyway, so `receive_some()`
  just needs *some* nonzero-progress contract.

```python
class QuicListener(trio.abc.Listener):
    '''
    Accepts iroh `Connection`s and yields one `QuicMsgStream`
    per accepted bi-stream, so `trio.serve_listeners()` spawns
    one `handle_stream_from_peer()` per `Channel`.

    '''
    async def accept(self) -> QuicMsgStream: ...
    async def aclose(self) -> None: ...
```

The accept-side subtlety: `trio.abc.Listener.accept()` yields
one stream per call, but iroh gives us *connections* which then
yield *streams*. So `QuicListener` needs an internal
`trio.MemoryReceiveChannel[QuicMsgStream]` fed by a background
task-pair (one task accepting connections, one per connection
accepting bi-streams). `trio.abc.Listener` has no nursery, so:
make the listener **constructed by an `@acm`** that owns the
nursery, and have `start_listener()` be that `@acm`'s driver.

⚠️ this collides with `Endpoint.start_listener()` being a plain
`async def` returning a listener. Two options:
- **(a)** hang the nursery off the `Endpoint`'s existing
  `listen_tn` — `_serve_ipc_eps()` already creates `listen_tn`
  and passes it into every `Endpoint` (`_server.py:1063-1074`),
  and `Endpoint.listen_tn` is right there. So
  `start_listener()` can `self.listen_tn.start_soon(...)` the
  acceptor tasks. **Recommended**: no upstream signature change,
  correct lifetime (dies with the ep group), and it's why
  `listen_tn` is on the struct in the first place.
- (b) change `start_listener()` to a `@acm`. Bigger blast
  radius; only if (a) proves insufficient.

Since `start_listener()` is called via
`inspect.getmodule(addr)` with only `addr=` (contract §1.3),
option (a) needs the `Endpoint` itself. Either add `ep=` to the
module-level `start_listener()` call signature (all backends
ignore it except quic → small upstream change, do it as part of
the prep PR and make it keyword-only with a default) or have
`QuicListener.accept()` lazily spawn via
`trio.lowlevel.current_task().parent_nursery` (**rejected** —
fragile, implicit). Do the explicit `ep=` kwarg.

### 3.4 `maddr`

Multiaddr already standardizes the pieces:

```
/ip4/<h>/udp/<p>/quic-v1                     # direct
/ip4/<h>/udp/<p>/quic-v1/p2p/<node-id>       # direct + identity
/dns/<relay-host>/tcp/443/tls/ws/p2p/<node>  # relay-ish
```

- primary form: `/p2p/<node-id>` alone is a legal maddr and is
  the *only* required component for iroh dialling — relay +
  direct addrs are discovery hints. So `mk_maddr()` emits
  `/p2p/<node_id>` and, when known, prefixes the direct
  `/ip4/../udp/../quic-v1/`.
- `/p2p/` values are multihash-encoded peer ids; an iroh node-id
  is a raw ed25519 key. Converting requires the identity
  multihash + libp2p key protobuf wrapper. **Decide**: emit the
  raw node-id under a *tractor-local* `/iroh/<node-id>` segment
  (needs upstream registration, same track as `wg`/`tipc`,
  gh #483) rather than pretending to be a libp2p peer-id we
  can't round-trip. Return the `str` form until upstream lands
  (`MsgTransport.maddr` is `Multiaddr|str`).
- this backend is the strongest argument for gh #443's
  **tunnelled/composed maddr** item: `/ip4/../udp/../quic-v1/..`
  *is* a composed stack. Cross-reference plan 03 §5 so the two
  grammars land compatibly.

---

## 4. Discovery integration

- iroh's node-id addressing means the `tractor` registrar can
  hold `IrohAddress`es that are **reachable from anywhere** with
  no port-forwarding — that is the headline feature. The
  registrar itself works unchanged.
- iroh has its own discovery (DNS/pkarr/mdns). **Out of scope**;
  note in the follow-up that `tractor.discovery` could
  eventually delegate to it, which would be the direct analogue
  of plan 01's TIPC-topology idea.
- relay servers: default to n0's public relays for the demo,
  document self-hosting (docs.iroh.computer's dedicated-infra
  page is linked from #353), and make the relay set a
  `start_listener()` kwarg.

## 5. Security note

QUIC is TLS-1.3-always and iroh authenticates by node-id, so
this backend is the first `tractor` transport with real
transport security and peer authentication. Two things follow:
1. an **allowlist hook** — an actor should be able to reject
   inbound connections from unknown node-ids *before* the
   `Aid` handshake. Natural home: a predicate kwarg on
   `start_listener()`, evaluated in `QuicListener`'s connection
   acceptor task. Sketch it; ship it in PR 1 if cheap (it is).
2. do **not** claim any security property for the other
   backends by association. `tcp`/`uds`/`tipc` remain
   unauthenticated; that's what plan 03 (wg) is for.

## 6. Commit sequencing

0. **spike (throwaway, not committed)**: drive iroh under
   `trio-asyncio`/`tractor.to_asyncio`, echo bytes over a
   bi-stream between two procs. Fills in §1.1. Timebox it.
1. prep PR: annotation widening + `rebind_from_sockname` gate +
   `transport_from_stream()` `tpt_key` dispatch + `ep=` kwarg on
   `start_listener()` + lazy `default_lo_addrs()`. **No new
   backend.** Full suite green on tcp *and* uds.
2. `_uniffi_trio.py` + its tests (drive one iroh async call
   under bare `trio.run()`; assert no asyncio loop; assert
   cancellation frees the future).
3. `QuicMsgStream` + tests against a *loopback* iroh endpoint
   pair in one process (no `tractor` runtime): send/recv, clean
   EOF → `b''`, reset → `BrokenResourceError`, use-after-close
   → `ClosedResourceError`.
4. `QuicListener` + `start_listener()` + `IrohAddress` +
   key-file mgmt.
5. `MsgpackQuicStream(MsgpackTransport)` + `connect_to()` +
   `maybe_open_context()` connection pooling.
6. registration tables + `--tpt-proto quic` + full suite.
7. maddr + docs + a two-host example (pairs with #482's format).

## 7. Testing

- capability predicate `is_quic_available()` → `iroh` importable
  *and* the uniffi driver symbol present at the pinned version.
  Same `pytest.fail`-early hook as plan 01 §7.2.
- **the acceptance bar is the same**: whole suite green under
  `--tpt-proto quic`. Expect this to shake out real bugs in the
  adapters (esp. teardown ordering and `TransportClosed`
  classification) — that's the point.
- expect to need **timeout headroom**: iroh endpoint bind +
  first connect (relay discovery) is orders of magnitude slower
  than a UDS bind. Before touching any test deadline, rule out
  the CPU-throttle false-positive (see the project's
  `env_cpu_throttle_masquerades_as_regression` note); then, if
  real, add a per-proto timeout multiplier to the test harness
  rather than editing individual tests.
- a no-network test mode: iroh with relays disabled +
  loopback direct addrs only, so CI doesn't depend on n0's
  infra. **Make this the default in CI**; mark the relay tests
  `pytest.mark.net` and keep them out of the default run.
- leak checks: assert every `SecretKey`/`Endpoint` is closed on
  actor teardown (an `Endpoint` left open holds UDP sockets and
  relay connections; a leak here shows up as hung tests, not
  errors).

## 8. Risks

| risk | mitigation |
| --- | --- |
| uniffi codegen internals shift on upgrade | pinned minor, symbol assertion at import, the "no asyncio loop" test, documented fallback to `to_asyncio` |
| rust-thread callback → trio wakeup mishandled (segfault / lost wakeup / un-cancellable task) | strong ref on the ctypes trampoline; `run_sync_soon` only; **bounded** shielded cancel-drain; run the `conc-anal` skill over the bridge |
| `iroh` wheel availability for 3.13/3.14 on linux+macos | verify in step 0; if missing, that alone may force the `aioquic` fallback |
| QUIC latency/jitter destabilizes the existing suite's timing assumptions | per-proto timeout multiplier, relay-less CI mode |
| `(str, str)` unwrapped form collides with UDS in `wrap_address()` | guarded case ordered first + explicit regression test (§3.2) |
| scope creep into iroh's docs/blobs/gossip crates | this backend is `Endpoint`+`Connection`+bi-streams only; anything else is a separate issue |

## 9. Follow-up issue seeds

- `tractor.discovery` delegating to iroh discovery (DNS/pkarr/mdns)
- per-`Context` QUIC sub-streams: today one `Channel` == one
  stream; QUIC would let each `tractor.Context` own its own
  stream with independent flow-control and cancellation — this
  is the genuinely novel win #353 gestures at, and it's a
  runtime-layer change, not a transport one
- unreliable QUIC datagrams for a lossy-ok broadcast transport
  (pairs with plan 01's TIPC-multicast seed)
- node-id allowlist → a real `tractor` authz story
- `aioquic` sans-io backend reusing §3's adapters
