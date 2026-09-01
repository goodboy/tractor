# Plan 02 — QUIC backend via `iroh` FFI, uniffi-async rewritten onto `trio`

Tracks gh [#353]. Prereq reading:
[`00_shared_backend_contract.md`](./00_shared_backend_contract.md).

**External-fact rule**: every claim here about `iroh`, UniFFI,
generated bindings, QUIC wire/security behavior, or multiaddr
support is provisional until the step-0 API-truth pass records a
source or probe. Tractor/Trio behavior read from this checkout is
the only locally proven basis for the plan.

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

Version pinning: treat API stability across `iroh` minors as an
**unverified external constraint** until step 0. Pin the version
exercised by the spike to `iroh>=X.Y,<X.Y+1` in a `quic` extra,
and **write down the exact resolved version + generated
`iroh/_uniffi*` module layout** in the module docstring, because
§2 depends on generated-code internals.

**Step 0 of implementation is an API-truth pass**: install the
pinned `iroh`, inspect both its generated Python and loaded FFI
symbols, and run the throwaway two-process spike. Record in
§1.1 the real names and observed contracts. Every statement
below about `iroh`, UniFFI, Rust callbacks, or generated symbols
is a **step-0 hypothesis**, not a locally proven fact, unless it
is copied into the completed API-truth table with a source or
probe. Tractor and Trio behavior cited from this checkout is not
subject to that qualifier.

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
| endpoint close + completion | `close()` / `await closed()` | |
| resolved node address | relay URL + direct socket addrs | |
| future start/poll callback ABI | generated symbols + args | |
| future cancel/complete/free | generated symbols + ordering | |
| callback quiescence guarantee | after poll/complete/free? | |
| cancellation terminal poll code | generated enum/value | |
| iroh exception/status taxonomy | per operation | |

---

## 2. The `trio`-native uniffi future bridge (`tractor/ipc/_uniffi_trio.py`)

### 2.1 Step-0 generated-ABI gate

The expected generated shape is: start an opaque Rust future,
poll it with a C callback, cancel through a generated cancel
symbol, consume its terminal value/status through `complete`,
then call `free`. The expected callback may arrive on a foreign
Rust thread. **All of that is external and provisional.** Step 0
must identify the exact generated driver and prove, from its
template/source plus probes:

1. the start, poll, cancel, complete, and free signatures for
   every return-type family used by `iroh`;
2. poll result values and whether callbacks can be synchronous,
   concurrent, repeated, or late;
3. which terminal state permits `complete`, when `free` is
   legal, and when no callback can still reference Python;
4. whether generated callback-data and call-status objects must
   remain alive, and how generated lifting/errors are applied;
5. whether one narrow generated async-driver entrypoint can be
   replaced without importing or requiring an asyncio loop.

Do not implement from a remembered UniFFI version. If cancel
does not have a documented path to a terminal, safely freeable
state, the native Trio bridge fails the spike gate and the first
backend uses the infected-asyncio fallback.

### 2.2 Cancellation-safe ownership

Do not let the caller task own a raw handle across an `await`.
Introduce an actor-scoped `UniffiFutureSupervisor` running in the
dedicated transport nursery specified in §3.2.1. That nursery
must span parent bootstrap, the service nurseries, and final
deregistration. For each call, its operation task owns the
**entire** generated lifecycle:

```text
create handle -> poll/callback loop -> complete -> lift/status
              -> free -> publish result
                 ^
                 cancel request uses generated cancel, then follows
                 the verified terminal poll/complete/free protocol
```

The operation task, not the awaiting caller, creates the handle.
Creation and insertion in the supervisor's live-operation set
must have no cancellation checkpoint between them. The operation
retains strong references to the C callback trampoline, callback
data, wake state, call status, and handle until step 0 proves all
callbacks are quiescent and `free` has returned. Use one stable
callback per operation unless the verified ABI requires a fresh
one per poll; in either case, retain every potentially callable
trampoline. Capture `current_trio_token()` in the Trio owner and
schedule the wake into Trio with `token.run_sync_soon(...)`; the
foreign callback only stores its poll result and schedules that
wake.

Caller cancellation is a request, not handle ownership transfer:

1. the caller sends an idempotent cancel request and waits under
   a short shield for the operation to acknowledge it;
2. the owner invokes the generated cancel function exactly once
   and continues the **verified** poll/complete/free sequence;
3. once caller cancellation is observed, cleanup completion never
   wins the race by returning a value. After acknowledgement the
   caller continues propagating its original Trio cancellation;
   if cleanup outlives the grace period it first abandons its
   result channel while the actor supervisor keeps ownership;
4. actor endpoint teardown stops accepting new calls, requests
   cancellation of all live operations, and joins the supervisor
   before destroying endpoint/key state.

There is deliberately no `move_on_after(...): free(handle)`
path. A timeout proves only that cleanup is slow; it does not
prove that callbacks are quiescent or that `free` is legal. A
wedged operation therefore remains visible in the supervisor and
can delay graceful actor shutdown; process-level termination is
the final escalation, not an unsafe FFI free.

Structured-concurrency race to test: caller cancellation may land
after handle creation, after each poll, during callback delivery,
after terminal readiness, during `complete`, and before result
publication. At every checkpoint exactly one operation task owns
the handle, exactly one `free` is possible, and the supervisor
cannot exit while that task or a callable trampoline remains.

### 2.3 how to apply it to the generated bindings

Do **not** fork/vendor the generated `iroh` Python. Subject to the
step-0 gate, ship a *narrow* re-dispatch shim:

- write `tractor/ipc/_uniffi_trio.py` with the supervisor and a
  `@cm patch_uniffi_for_trio()` that patches only the generated
  async-driver entrypoint recorded in §1.1;
- verify at import time that the expected symbol exists and
  raise a clear, actionable error naming the pinned `iroh`
  version if not. A silent fallback to asyncio would be a
  nightmare to debug.
- treat every `iroh`/UniFFI upgrade as requiring the step-0 ABI
  gate again. Keep a test that drives one trivial call under bare
  `trio.run()`, asserts no asyncio loop, and injects cancellation
  at every lifecycle checkpoint. Point the module docstring at
  the exact generated template/revision mirrored by the shim.

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

**Connection pooling** is actor-endpoint state, never module
state. Its key is exactly
`(local_endpoint_identity, remote_node_id, alpn)`, where local
endpoint identity is the local NodeId derived from the actor key.
Remote NodeId alone would incorrectly share connections across
local keys or protocol epochs. Build it over the codebase's
`maybe_open_context()` idiom only after a concurrency review of
its actual last-user teardown behavior in the implementation
revision. Do not assume an issue reference proves the required
ordering.

`acquire_connection()` returns a `ConnectionLease`, not a bare
connection. An outgoing `QuicMsgStream` owns that entered lease
for its whole lifetime; `connect_to()` must not exit the cached
context immediately after `open_bi()`. Exact transfer paths:

- dial/acquire or `open_bi()` failure releases the lease in a
  shielded `finally` before raising;
- successful stream construction atomically transfers the lease
  to `QuicMsgStream` before the first cancellation checkpoint;
- `send_eof()` closes only the send half and does not release;
- clean receive EOF closes only the receive half and does not
  release while the send half remains usable;
- one guarded terminal-state transition releases exactly once
  when both halves have become terminal, in either order;
- `aclose()`, reset, or terminal connection failure closes both
  halves as applicable and idempotently releases exactly once;
- a stream queued by `QuicListener` already owns its lease; if
  never accepted, listener draining closes it and releases it.

After `accept()` returns, the server dispatch path owns the stream
until a handler task starts and must close it if task start fails.
The handler then takes ownership, with an outer `finally` that
calls `stream.aclose()` on normal return, handshake failure, and
cancellation. Lease release itself is an idempotent pool state
transition; if last-user connection teardown awaits FFI, the actor
endpoint's pool supervisor owns that await so cancellation of the
handler cannot strand the lease.

For inbound connections, the connection-feeder owns a base lease
while accepting streams and each queued/returned stream gets a
child lease. The base lease is released only after the accept
loop ends; the pool closes the connection after the base and all
stream leases are gone. Reject or deterministically reconcile a
simultaneous inbound/outbound duplicate for the same full key;
record the chosen iroh-compatible rule during step 0.

### 3.2 `IrohAddress`

```python
class IrohAddress(
    msgspec.Struct,
    frozen=True,
):
    _node_id: str
    _alpn: str
    _relay_url: str|None
    _direct_addrs: tuple[str, ...]

    proto_key: ClassVar[str] = 'quic'
    unwrapped_type: ClassVar[type] = tuple
    def_bindspace: ClassVar[str] = 'tractor/0'
```

- **`.unwrap()` is the complete, tagged wire descriptor**:
  `('quic', node_id, alpn, relay_url, direct_addrs)`. All values
  are msgpack-native and `direct_addrs` is canonicalized to a
  tuple. `from_addr()` requires that exact tag and shape; never
  infer QUIC from a `(str, str)` pair. This depends on the shared
  contract's tagged-address migration and removes the UDS
  collision rather than ordering around it.
- The descriptor always carries NodeId, ALPN, and both route-hint
  fields. For this discovery-free first backend, `.is_valid`
  requires a parseable NodeId, non-empty ALPN, and at least one
  relay URL or direct address. Whether NodeId-only dialing works
  through optional iroh discovery is a step-0 API check and is
  not part of the first implementation.
- `.bindspace` → `self._alpn`. This is the honest analogue:
  the ALPN is the set of endpoints willing to talk to you, and
  two `tractor` deployments sharing an iroh network are
  separated by ALPN exactly as two UDS deployments are
  separated by directory. Include a `tractor` version/proto
  epoch in the default ALPN so incompatible runtimes can't
  handshake.

### 3.2.1 One actor endpoint and key

Add an actor-scoped `QuicActorEndpoint` resource containing the
secret key, one bound iroh endpoint, the UniFFI supervisor, the
connection pool, and its latest resolved `IrohAddress`. It cannot
live in `_service_tn`: a child dials its parent before that nursery
opens, while final deregistration may dial after it closes.

Add a dedicated `transport_tn` around the complete actor runtime:
the task that opens this nursery must start the complete
`async_main` sequence as a **child** of it and wait for that child.
That makes `transport_tn` an ancestor of every parent-dial,
service, and deregistration caller, satisfying
`maybe_open_context(tn=transport_tn)` rather than asking the
nursery-opening task to use its own child nursery. The child keeps
the nursery around `_root_tn` and `_service_tn`, performs final
deregistration while it remains open, then returns so the owner can
close the transport resource and nursery. Root startup needs the
equivalent outer owner around actor construction, service, and
teardown. If this shape cannot be preserved, the connection pool
must stop depending on `maybe_open_context()`'s ancestor-nursery
contract. No path creates a second endpoint for the actor.

The child currently receives transport configuration only in the
`SpawnSpec` sent over its already-open parent channel. QUIC cannot
derive its local key, ALPN, or requested bind policy from that late
message. Add a small msgpack/pickle-native
`ChildTransportBootstrap` to every process-launch path. It carries
the selected protocol and the QUIC-local key reference/generation
policy, ALPN, relay policy, and requested bind constraints. It is
available before `_from_parent()`; the later `SpawnSpec` repeats
the public configuration and startup rejects any mismatch. Root
actors derive the same bootstrap record directly from
`open_root_actor()` inputs before address selection.

With that prep in place, the order is:

1. consume the launch-time bootstrap record, select one key
   (persisted and explicitly provisioned for a registrar, fresh
   for an ordinary actor), and construct/bind the endpoint in the
   transport owner task;
2. await the step-0-verified address-ready API and build a valid
   descriptor from the endpoint's NodeId, ALPN, relay URL, and
   direct addresses;
3. only then dial `_from_parent()` through this endpoint;
4. start `QuicListener` over this endpoint's accept API;
5. publish the resolved descriptor as `Endpoint.addr` and
   `Actor.accept_addrs` before parent/registrar registration;
6. after service nurseries close, keep the endpoint available for
   deregistration; then close listeners and streams, drain
   connection leases and FFI operations, close/join the endpoint,
   and release key state.

`IrohAddress.get_random()` is therefore a descriptor lookup on
the active actor transport resource, not key generation. Broaden
the shared `get_random()` contract for resource-backed transports
and make root/subactor address selection consume the bootstrap
resource instead of calling it before that resource exists. Do not
hide a secret in a module-level side table. Calls without an active
resource fail clearly rather than allocating an unowned key.

`get_root()` never returns an empty/sentinel NodeId. Make default
addresses lazy, and have QUIC load a provisioned public registrar
descriptor. Registrar provisioning writes its secret separately
with mode 0600 and writes the matching complete public descriptor
atomically; endpoint startup verifies the derived NodeId. If no
descriptor exists, default QUIC registrar discovery fails with an
actionable configuration error. Automatic first-process election
is deferred until a safe key-file locking and endpoint-binding
protocol is proven; key generation never occurs in the listen
path.

#### 3.2.2 `proto_key`: `'iroh'` vs `'quic'`

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

    def __init__(self, conn, send, recv, lease) -> None: ...
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
- honour Trio's one-task-per-direction rule with public,
  implementation-local guards that raise
  `trio.BusyResourceError`; do not depend on `trio._util`.
  `MsgpackTransport` already serializes sends, while receives are
  single-task by construction.
- **buffering**: if iroh's `read()` doesn't support
  "read up to n", `receive_some()` must maintain an internal
  leftover buffer. Note `MsgpackTransport` wraps us in
  `tricycle.BufferedReceiveStream` anyway, so `receive_some()`
  just needs *some* nonzero-progress contract.

Centralize exception translation at every iroh/UniFFI boundary;
no generated exception may escape into `Channel` or server code.
Step 0 must record actual exception classes/status payloads and
build an exhaustive operation-specific mapping:

| observed condition | adapter result |
| --- | --- |
| receive clean EOF | `b''` |
| local stream/listener/endpoint already closed | `trio.ClosedResourceError` |
| concurrent same-direction operation | `trio.BusyResourceError` |
| peer reset, stopped stream, lost connection | `trio.BrokenResourceError` |
| dial rejected or no usable route | `ConnectionRefusedError` or `ConnectionError` |
| caller's Trio deadline/cancellation | preserve Trio cancellation semantics |
| unexpected FFI status/panic | chained `RuntimeError` identifying operation and pinned version |

Preserve the original exception as `__cause__`, but sanitize
messages so `_transport.py` sees stable Trio/Tractor categories,
not version-specific iroh text. Endpoint accept failure becomes a
listener `BrokenResourceError`; normal endpoint shutdown becomes
`ClosedResourceError`. Add one test per observed step-0 status.

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

The accept-side subtlety is fan-out: one actor transport accepts
connections and each connection accepts streams, while
`Listener.accept()` returns one stream. Give **each** listener a
supervisor task started with
`await server_ep.listen_tn.start(...)`.
That task creates and owns a cancel scope, a child nursery for the
endpoint feeder plus per-connection feeders, a guarded stream
queue, and a completion event.
`start_listener(addr=, server_ep=, actor_tpt=)` does not return
until the supervisor has reported all of those ready.
Do not borrow an implicit parent nursery or spawn feeders lazily
from `accept()`.

The queue is a guarded `deque`, not an unowned memory-channel
buffer. A feeder transfers a fully constructed, lease-owning
stream into it only while the listener is open; if close wins the
race, the feeder closes the stream itself. `accept()` atomically
pops one item or waits on the queue condition. Once close is
marked and the queue is empty, it raises
`trio.ClosedResourceError`.

`QuicListener.aclose()` is idempotent and has this exact order:

1. under the queue guard, mark closed and wake all `accept()`
   waiters without a checkpoint between the state change and
   notification;
2. cancel the listener-owned supervisor scope;
3. the supervisor's shielded `finally` joins the endpoint and all
   connection feeders, atomically detaches the queue, closes every
   queued stream, releases their leases, and closes the queue;
4. only after that finalizer finishes, the supervisor sets its
   completion event;
5. `aclose()` waits under a shield for that event and returns;
   concurrent closers wait for the same event.

The same supervisor finalizer runs if its parent nursery is
cancelled before someone calls `aclose()`. This makes the
supervisor, not an arbitrarily cancelled caller, the sole final
cleanup owner. Test cancellation at feeder accept, stream
construction, queue transfer, `accept()` wakeup, and each close
checkpoint; no feeder may outlive the listener and no queued
lease may survive completion.

This needs two explicit, typed references in the module-level
listener call: `server_ep=` is the IPC server `Endpoint` that owns
`listen_tn`, while `actor_tpt=` is the already-open
`QuicActorEndpoint` whose iroh accept API supplies connections.
Store `actor_tpt` on the server endpoint during actor transport
bootstrap and pass both keyword-only arguments; socket backends
ignore `actor_tpt`. `Endpoint.start_listener()` then stores the
listener's already-resolved address instead of calling
`getsockname()`.

### 3.4 `maddr`

Expected multiaddr spellings for direct QUIC and relay routes are
**step-0 verification items**, not assumptions:

```
/ip4/<h>/udp/<p>/quic-v1                     # direct
/ip4/<h>/udp/<p>/quic-v1/p2p/<node-id>       # direct + identity
/dns/<relay-host>/tcp/443/tls/ws/p2p/<node>  # relay-ish
```

- Do not emit NodeId alone in the first backend: without enabled
  discovery it would discard the route required by the complete
  `IrohAddress`. `mk_maddr()` must preserve NodeId, ALPN, relay
  URL, and all direct addresses, or return a canonical Tractor
  string form that does until a multiaddr grammar can round-trip
  every field.
- Verify whether an iroh NodeId can losslessly map to `/p2p/`.
  If not, use a tractor-local `/iroh/<node-id>` segment rather
  than pretending to be a libp2p peer-id. This needs upstream
  registration, on the same track as `wg`/`tipc` (gh #483).
- this backend is the strongest argument for gh #443's
  **tunnelled/composed maddr** item: `/ip4/../udp/../quic-v1/..`
  *is* a composed stack. Cross-reference plan 03 §5 so the two
  grammars land compatibly.

---

## 4. Discovery integration

- The registrar stores the complete `IrohAddress`, not only a
  NodeId. Registration is forbidden until endpoint address
  resolution has produced that descriptor. If route hints change
  later, dynamic re-registration is a follow-up; the spike uses
  the pre-registration snapshot.
- Optional iroh discovery mechanisms and their names/capabilities
  are step-0 verification items and out of scope for the first
  backend. No NodeId-only reachability claim is made.
- Relay configuration belongs to `QuicActorEndpoint` creation,
  not `start_listener()`, because dialing and listening reuse the
  same endpoint. The demo's relay choice and self-hosted option
  are selected only after step 0 verifies the pinned API.

## 5. Security note

The transport-security and NodeId-authentication properties of the
pinned iroh stack are **step-0 documentation-verification items**.
Claim only the properties supported by that version's source and
docs. Two design consequences remain:
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
   bi-stream between two processes. Fill §1.1 with generated ABI,
   endpoint resolution, close/join, and error observations. Probe
   cancel at every generated lifecycle phase. Timebox it and use
   the fallback if any mandatory ownership fact stays unknown.
1. prep PR: tagged address migration, annotation widening,
   non-socket listener reconciliation, `tpt_key` dispatch,
   typed `server_ep=`/`actor_tpt=` listener inputs, and lazy
   default addresses. **No new backend.** Keep tcp and uds
   behavior unchanged.
2. bootstrap prep: pass `ChildTransportBootstrap` through every
   process-launch path and add the transport nursery around child
   parent-dial, service, deregistration, and teardown. Resolve the
   endpoint address before registration. Add no iroh-specific
   global state.
3. `_uniffi_trio.py` supervisor + lifecycle fault-injection tests:
   no asyncio loop, one owner/complete/free, callback retention,
   bounded caller handoff, and joined durable cleanup.
4. `QuicActorEndpoint` + provisioned registrar descriptor +
   loopback direct-address tests; prove one endpoint handles dial,
   listen, address lookup, and ordered teardown.
5. `QuicMsgStream` + exhaustive error-normalization and lease
   release tests against the loopback endpoint pair.
6. `QuicListener` supervisor + cancellation-at-every-checkpoint
   tests, including queued-stream draining and feeder joins.
7. `MsgpackQuicStream`, full-key connection pooling, registration
   tables, and `--tpt-proto quic`; then run the full suite.
8. routable maddr/string form + docs + a two-host example (pairs
   with #482's format).

## 7. Testing

- capability predicate `is_quic_available()` → `iroh` importable
  *and* every step-0-recorded driver symbol present at the pinned
  version. Same `pytest.fail`-early hook as plan 01 §7.2.
- **the acceptance bar is the same**: whole suite green under
  `--tpt-proto quic`. Expect this to shake out real bugs in the
  adapters (esp. teardown ordering and `TransportClosed`
  classification) — that's the point.
- Measure endpoint bind and first-connect latency in step 0; do
  not assume a multiplier. Before changing a deadline, rule out
  the project's CPU-throttle false-positive, then prefer one
  per-proto harness multiplier over individual-test edits.
- Use the step-0-verified relay-disable configuration with direct
  loopback addresses for default CI. Mark separately verified
  relay tests `pytest.mark.net` and keep them out of default CI.
- leak checks: assert the actor has one key/endpoint, every FFI
  operation completed/freed once, all listener feeders joined,
  all queued streams closed, every connection lease released,
  and endpoint close completion observed before actor teardown.
- address-ordering check: block registration until a descriptor
  with NodeId, ALPN, and at least one route is published; reject
  sentinel, NodeId-only, and post-registration mutation cases.

## 8. Risks

| risk | mitigation |
| --- | --- |
| uniffi codegen internals shift on upgrade | pinned minor, symbol assertion at import, the "no asyncio loop" test, documented fallback to `to_asyncio` |
| callback wakeup/lifetime semantics differ from the hypothesis | step-0 source + probe gate; retain callback/data through verified quiescence; durable owner; never timeout-free |
| cancelled foreign future never reaches a freeable state | bounded caller handoff to visible actor supervisor; joined graceful shutdown or process-level escalation; never speculative free |
| `iroh` wheel availability for 3.13/3.14 on linux+macos | verify in step 0; if missing, that alone may force the `aioquic` fallback |
| endpoint or route resolution is not ready before parent dial/registration | actor endpoint bootstrap barrier; publish only a complete resolved descriptor |
| connection closes while a stream still uses it | full-key pool + stream-held leases + exact-once release tests |
| listener close strands feeder tasks or queued streams | listener-owned scope/completion event; cancel, join, drain, then return |
| QUIC latency/jitter destabilizes suite timing assumptions | measure first; per-proto multiplier only if demonstrated; relay-less CI mode |
| address tuple collides with another backend | required `'quic'` tag and exact-shape dispatch |
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
