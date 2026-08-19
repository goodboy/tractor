# Plan 01 — `TIPC` transport backend (`tractor/ipc/_tipc.py`)

Tracks gh [#378]. Prereq reading:
[`00_shared_backend_contract.md`](./00_shared_backend_contract.md).

**Thesis**: TIPC is the *cheapest* new backend we can add and
simultaneously the only one that gives us cluster-wide service
discovery **for free, in the kernel**, replacing (for
TIPC-capable deployments) the whole `tractor.discovery`
registrar round-trip with a `bind()`/`connect()` on a
*service name*. It is stdlib-only: zero new dependencies.

[#378]: https://github.com/goodboy/tractor/issues/378

---

## 1. Why this is small: three verified facts

1. **CPython already speaks TIPC.** `socket.AF_TIPC` plus 23
   `TIPC_*` constants are present in the stdlib on Linux
   (verified on the dev box, py3.13):
   `AF_TIPC, SOL_TIPC, TIPC_ADDR_ID, TIPC_ADDR_NAME,
   TIPC_ADDR_NAMESEQ, TIPC_CFG_SRV, TIPC_CLUSTER_SCOPE,
   TIPC_CONN_TIMEOUT, TIPC_{CRITICAL,HIGH,MEDIUM,LOW}_IMPORTANCE,
   TIPC_DEST_DROPPABLE, TIPC_IMPORTANCE, TIPC_NODE_SCOPE,
   TIPC_PUBLISHED, TIPC_SRC_DROPPABLE, TIPC_SUBSCR_TIMEOUT,
   TIPC_SUB_CANCEL, TIPC_SUB_PORTS, TIPC_SUB_SERVICE,
   TIPC_TOP_SRV, TIPC_WAIT_FOREVER, TIPC_WITHDRAWN,
   TIPC_ZONE_SCOPE`.
   `sock.bind()/connect()/getsockname()` take/return the
   5-tuple `(addr_type, v1, v2, v3, scope)` — the last element
   is optional on input and defaults to `0`.
2. **`trio` doesn't care about the address family.** Per
   contract §1.5, `trio.SocketStream` and `trio.SocketListener`
   only require a trio socket object of type `SOCK_STREAM`.
   TIPC's `SOCK_STREAM` is a real connection-oriented reliable
   byte stream. So we reuse `trio.SocketStream`,
   `trio.SocketListener`, `trio.serve_listeners()`,
   `MsgpackTransport`'s framing — *all of it*.
3. **It is not available by default.** On this box
   `socket.socket(AF_TIPC, SOCK_STREAM)` →
   `OSError(97, 'Address family not supported by protocol')`
   with no `tipc` in `/proc/modules`. `modprobe tipc` is
   required; cross-node needs a bearer
   (`tipc bearer enable media eth device <if>` or
   `media udp name <n> localip <ip>`). Everything about this
   plan's testability hinges on gating (§7).

Non-goals: `SOCK_RDM`/`SOCK_DGRAM`/`SOCK_SEQPACKET` message
modes, multicast fan-out, and TIPC group messaging. They are
genuinely interesting for a future `tractor` broadcast/pubsub
transport but they do **not** fit `MsgTransport`'s
stream-of-length-prefixed-msgs shape. Note them in the
follow-up issue, do not build them here.

---

## 2. `TIPCAddress`

### 2.1 the three TIPC address flavours, and which we use

| flavour | tuple | meaning |
| --- | --- | --- |
| `TIPC_ADDR_NAMESEQ` | `(type, lower, upper, scope)` | a *published range* — what a server `bind()`s |
| `TIPC_ADDR_NAME` | `(type, instance, domain, scope)` | a *lookup* — what a client `connect()`s |
| `TIPC_ADDR_ID` | `(node, ref, 0, scope)` | a concrete port id — the "physical" address |

The design decision that makes this backend coherent:

> **A `tractor` actor's TIPC address is a *service name*
> `(type, instance)`; `bind()` publishes the singleton range
> `(type, instance, instance)`; peers `connect()` by name and
> the kernel resolves + load-balances. `TIPC_ADDR_ID` is only
> ever an *observed* address (`getpeername()`), never a
> user-facing one.**

This is exactly the "leverage the built-in discovery machinery"
ask in #378: publishing a bind *is* registration, and
`connect()` on a name *is* a lookup, with no registrar actor in
the loop.

### 2.2 the struct

```python
class TIPCAddress(
    msgspec.Struct,
    frozen=True,
):
    _stype: int                    # TIPC "type" == service class
    _instance: int                 # service instance within the type
    _scope: int = TIPC_CLUSTER_SCOPE
    # observed-only, never part of identity/equality-by-intent
    maybe_node: int|None = None    # from TIPC_ADDR_ID getpeername()
    maybe_ref: int|None = None

    proto_key: ClassVar[str] = 'tipc'
    unwrapped_type: ClassVar[type] = tuple[str, int, int, int]
    def_bindspace: ClassVar[int] = TIPC_CLUSTER_SCOPE
```

**Unwrapped form** (the wire/`SpawnSpec` shape).

TIPC's natural form is `(stype, instance, scope)` — but a
2-tuple squeeze of it is a `(str, int)`, i.e. *the same coarse
shape as `TCPAddress`*, so `wrap_address()`'s
`case (str(), int())` steals it. This backend is therefore the
forcing function for the contract-doc's conclusion (§1.1):

> **make the unwrapped form carry an explicit proto-key, spelled
> with the `multiaddr` protocol name.**

```python
def unwrap(self) -> tuple[str, int, int, int]:
    return ('tipc', self._stype, self._instance, self._scope)
```

`wrap_address()` then dispatches `_address_types[addr[0]]` and
the collision class disappears. **This is a prerequisite
migration commit, not part of this backend** — see contract §1.1
for its blast radius (wire format + every fixture + `piker`
config) and for the follow-on "stop handing raw tuples to users
at all, à la `ipaddress`" direction.

⚠️ an earlier revision of this plan proposed a self-tagging
`('tipc:<stype>:<scope>', instance)` string-prefix hack with an
ordered `case` guard. **Dropped** — it papers over the problem,
keeps `wrap_address()` order-sensitive, and doesn't help iroh's
`(str, str)`-vs-UDS collision at all. Do not resurrect it.

Note `TIPCAddress` is the first backend where `.unwrap()` is
**not** a lossless view of the live socket — `maybe_node`/
`maybe_ref` are observed metadata, exactly like
`UDSAddress.maybe_pid` (which is likewise excluded from
`.unwrap()`). Follow that precedent, including its `__repr__`
treatment (`_uds.py:242`).

### 2.3 how to pick `_stype` and `_instance`

- `_stype` = a `tractor`-reserved service class. TIPC reserves
  0..63 for internal use (`TIPC_TOP_SRV == 1`,
  `TIPC_CFG_SRV == 0`). Use a module constant
  `TRACTOR_STYPE: int = 0x74_72_00_00` ("tr\0\0") as the default
  and make it overridable via `TIPCAddress._stype` so an app
  can partition service classes. Document that two `tractor`
  trees sharing a cluster **and** a `_stype` share a namespace.
- `_instance` for `get_root()`: `1616` — mirrors the
  `TCPAddress.get_root()` port and the `registry@1616.sock`
  UDS filename, so the "1616 is tractor's registrar" idiom
  holds across all backends.
- `_instance` for `get_random()`: TIPC gives us no
  kernel-assigned-instance analogue of `port=0`, so we must
  choose. Use a *pure* fn of the actor identity so it is
  reproducible and collision-free:
  ```python
  # 32-bit instance derived from the actor's uuid4 (+ pid when
  # there's no live runtime, per the UDS precedent).
  inst: int = int.from_bytes(
      blake2b(seed.encode(), digest_size=4).digest(),
      'big',
  )
  ```
  where `seed = f'{actor.aid.name}@{pid}'` if
  `current_actor(err_on_no_runtime=False)` else
  `f'{prefix}.{uuid4().hex[:8]}@{pid}'`. Must avoid the reserved
  low range: `inst = 64 + (inst % (2**32 - 64))`.
  ⚠️ *unlike* `port=0`, a collision here surfaces as a
  successful-but-shared publication (TIPC allows multiple
  binders on the same name and round-robins!) rather than
  `EADDRINUSE`. That is a silent-crosstalk failure mode; §7 has
  the test that proves the 4-byte digest is enough and §9 has
  the mitigation if it isn't.
- `_scope`: `TIPC_NODE_SCOPE` for a same-host-only actor (the
  UDS-equivalent), `TIPC_CLUSTER_SCOPE` (default) for
  cluster-visible. **This is `.bindspace`**:
  ```python
  @property
  def bindspace(self) -> int:
      return self._scope
  ```
  It is the honest analogue of "the set of hosts this bind is
  reachable from", which is precisely the docstring in
  `Address.bindspace`. (`TIPC_ZONE_SCOPE` is deprecated/aliased
  to cluster in modern kernels — accept it on input, normalize
  to cluster, log at `transport` level.)

### 2.4 `is_valid`

```python
@property
def is_valid(self) -> bool:
    return (
        self._instance != 0
        and
        self._stype not in _tipc_reserved_stypes  # {0, 1, ...}
        and
        self._scope in (TIPC_NODE_SCOPE, TIPC_CLUSTER_SCOPE)
    )
```

---

## 3. Listener + stream

### 3.1 `start_listener()`

```python
async def start_listener(
    addr: TIPCAddress,
    backlog: int = 128,
    **kwargs,
) -> SocketListener:
    sock = trio.socket.socket(
        socket.AF_TIPC,
        socket.SOCK_STREAM,
    )
    # publish the singleton name-range == "register the service"
    await sock.bind((
        socket.TIPC_ADDR_NAMESEQ,
        addr._stype,
        addr._instance,
        addr._instance,
        addr._scope,
    ))
    sock.listen(backlog)
    return SocketListener(sock)
```

Notes / hazards:

- `bind()` on `AF_TIPC` is **not** a filesystem or port-table
  operation and can't block on DNS, but keep it `await`ed
  through `trio.socket` anyway for uniformity.
- `backlog=128` matching `_uds.start_listener()`'s hard-won
  value (see its comment at `_uds.py:317-331` re: concurrent
  deregistration storms). Do not use `1`.
- **no `close_listener()` needed** — nothing to unlink. Omit the
  function entirely (contract §1.2: absence means implicit).
  Withdrawal of the published name happens on socket close.
- ✅ **SETTLED** (step-0 probe, live kernel): `SocketListener.
  __init__`'s `getsockopt(SOL_SOCKET, SO_ACCEPTCONN)` **works**
  on `AF_TIPC` and answers `1`. We do *not* rely on trio's
  `except OSError: pass` carve-out at all. Pinned by
  `test_listener_tolerates_so_acceptconn`.
- Wrap the bind in a `_reraise_as_connerr()`-style `@cm` (copy
  the `_uds.py:256` pattern) so `EADDRINUSE`-ish and
  `EAFNOSUPPORT` become `ConnectionError` with the addr in the
  message. `EAFNOSUPPORT` here means "kernel module not
  loaded" and deserves a *specifically actionable* message:
  `'TIPC unavailable — try `sudo modprobe tipc`\n'`.

### 3.2 the `getsockname()` reconciliation

`Endpoint.start_listener()` does
`if lstnr.socket.getsockname() != self.addr.unwrap(): self.addr =
self.addr.from_addr(unwrapped)`.

For TIPC, `getsockname()` on a bound-but-listening socket
returns a `TIPC_ADDR_ID`-flavoured 5-tuple (the port id), *not*
the name-seq we bound. So the `!=` is **always true** and
`from_addr()` will be handed a 5-tuple.

Handle it inside `TIPCAddress.from_addr()` — do **not** patch
`_server.py`:

⚠️ the sketch that stood here used the `'tipc:<stype>:<scope>'`
string-prefix hack §2.2 explicitly **withdrew**. Corrected to
the proto-keyed form (and note a bare seq-pattern matches the
`list` that `msgpack` decodes our tuples back to, so no
separate `[...]` alternative is needed):

```python
@classmethod
def from_addr(cls, addr) -> TIPCAddress:
    match addr:
        # our own proto-keyed unwrapped form
        case ('tipc', int() as stype, int() as inst, int() as scope):
            return TIPCAddress(stype, inst, _norm_scope(scope))

        # ..w/ the scope defaulted
        case ('tipc', int() as stype, int() as inst):
            return TIPCAddress(stype, inst)

        # a kernel-observed TIPC_ADDR_ID 5-tuple
        case (int() as atype, *_) if atype == TIPC_ADDR_ID:
            ...
```

The `TIPC_ADDR_ID` case cannot reconstruct `(stype, instance)`
— that info isn't in a port id. So `from_addr()` alone is
insufficient for the reconciliation path. **Resolution**: make
`from_addr()` raise a clear `ValueError` for the bare
`TIPC_ADDR_ID` case, and instead prevent the reconciliation
from firing by having `start_listener()` return a listener
whose `getsockname()` we never need — i.e. land this two-line
upstream fix in `_server.py:664`:

```python
if (
    (unwrapped := lstnr.socket.getsockname()) != self.addr.unwrap()
    and
    self.addr.rebind_from_sockname   # ClassVar[bool] = True on tcp/uds
):
```

with `TIPCAddress.rebind_from_sockname: ClassVar[bool] = False`
(and `True` on `TCPAddress`/`UDSAddress`, preserving today's
behaviour exactly). Rationale: the reconciliation exists *only*
to learn the kernel-assigned port for `port=0` TCP binds (its
own comment says so, `_server.py:662`); TIPC has no such
late-binding, so opting out is semantically right rather than a
hack. **Land this as its own commit, ahead of the backend**,
with a test that `tcp`'s `port=0` behaviour is unchanged.

Keep the observed port-id available anyway: annotate
`ep.addr = ep.addr.with_port_id(*getsockname()[1:3])` (a pure
`msgspec.structs.replace()` helper) purely for logging/repr.

### 3.3 `MsgpackTIPCStream`

```python
class MsgpackTIPCStream(MsgpackTransport):
    address_type = TIPCAddress
    layer_key: int = 4

    @property
    def maddr(self) -> Multiaddr|str:
        return mk_maddr(self.raddr)

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1

    @classmethod
    async def connect_to(
        cls,
        destaddr: TIPCAddress,
        prefix_size: int = 4,
        codec: MsgCodec|None = None,
        **kwargs,
    ) -> MsgpackTIPCStream:
        sock = trio.socket.socket(AF_TIPC, SOCK_STREAM)
        with close_on_error(sock):
            # NOTE: connect by *name* -> kernel does the lookup,
            # so this is our "discovery" call.
            await sock.connect((
                socket.TIPC_ADDR_NAME,
                destaddr._stype,
                destaddr._instance,
                0,              # domain: 0 == "anywhere in scope"
                destaddr._scope,
            ))
        return cls(
            trio.SocketStream(sock),
            prefix_size=prefix_size,
            codec=codec,
        )
```

- reuse `trio._highlevel_open_unix_stream.close_on_error` (the
  UDS backend already imports it) or inline the equivalent
  `try/except: sock.close(); raise`.
- `SO_/TIPC_` opts worth setting and documenting:
  - `setsockopt(SOL_TIPC, TIPC_IMPORTANCE, TIPC_HIGH_IMPORTANCE)`
    for the *parent<->child* lifetime channel — this is a real
    win TIPC gives us that TCP can't: the runtime's
    supervision channel can outrank bulk app traffic under
    congestion. Wire it as a `connect_to(..., importance=...)`
    kwarg defaulted from a module constant, and have
    `_runtime.py`'s parent-chan path pass the high value **in a
    follow-up** (don't couple it to this PR).
  - `TIPC_CONN_TIMEOUT` — the kernel-side connect timeout;
    leave at default, we have `trio` cancel scopes.
  - `TIPC_DEST_DROPPABLE = 0` on the connection so undeliverable
    msgs come back as errors rather than being silently dropped.
- ✅ **SETTLED** — **`connect_to()` on a name with no
  publisher**: TIPC answers `EHOSTUNREACH` (113) *instantly*
  (no SYN-timeout wait), which is indeed better discovery-ping
  behaviour than TCP.

  ⚠️ BUT the errno matters more than expected: python maps
  `EHOSTUNREACH` to a **bare `OSError`**, NOT to a
  `ConnectionError` subtype the way it maps `ECONNREFUSED` ->
  `ConnectionRefusedError`. So the `_reraise_as_connerr()` wrap
  is **load-bearing for contract §4**, not cosmetic polish —
  without it the registrar ping path sees a foreign exc type.
  (For contrast, dialling a bogus *port-id* — as opposed to a
  name — does give `ECONNREFUSED`.)

### 3.4 `get_stream_addrs()`

```python
@classmethod
def get_stream_addrs(cls, stream) -> tuple[TIPCAddress, TIPCAddress]:
    sock = stream.socket
    # both return TIPC_ADDR_ID 5-tuples for a connected sock
    l_id = sock.getsockname()
    r_id = sock.getpeername()
    ...
```

Problem: neither end's port-id tells us the *service name*. The
`laddr`/`raddr` are used for logging, `Channel.raddr`,
`Server._peers` keying-adjacent repr, and `maddr`. Design:

- the **connecting** side knows the destaddr it dialled →
  `connect_to()` overrides `_raddr` after construction with the
  known-good `TIPCAddress`, exactly as
  `MsgpackUDSStream.connect_to()` does for the peer-pid case
  (`_uds.py:539-543`).
- the **accepting** side does not know the peer's service name
  from the socket. Two honest options:
  - **(a) accept it: `raddr` carries only `(node, ref)`** via
    `maybe_node`/`maybe_ref`, `_stype/_instance` set to a
    sentinel `-1`, and `__repr__` renders
    `TIPCAddress[<peer-node:0x...>:<ref>]`. The `Aid` from the
    handshake already gives us the peer's logical identity, so
    nothing in the runtime actually *needs* the peer's service
    name. **Recommended.**
  - (b) piggyback the peer's own bound name in the handshake.
    Rejected for this PR: touches `Aid`/msg-spec.
- `laddr` on the accepting side: the `Endpoint` knows its own
  `addr`; but `get_stream_addrs()` is a `@classmethod` with only
  the stream. Use `TIPC_ADDR_ID` for `laddr` too and let
  `Endpoint.peer_tpts` keying (which is by *peer* addr) still
  work. Verify nothing asserts `laddr == ep.addr` — grep for
  `.laddr` uses before committing (`_server.py`'s
  `con_status` logging, `Channel.pformat()`).
  ✅ grepped: `.laddr` is repr/logging-ONLY. `.raddr` has three
  real consumers (`discovery/_api.py:277`'s `query_actor()`
  yield, plus two test asserts) — and note `uds` *already* has
  this same wart (its accepting-side `raddr` is the listener's
  own sockpath), so (a) is consistent with the status quo.

- 🐛 **HAZARD the original draft missed — a dropped peer must
  not kill the actor.** Unlike tcp/uds — where the kernel keeps
  answering the peer addr until *we* close — a TIPC socket
  whose peer has already gone answers **`ENOTCONN`** from
  `getpeername()`.

  That's fatal as written, because
  `MsgpackTransport.__init__()` calls `get_stream_addrs()` (via
  `Channel.from_stream()`) **before** the handshake, so the
  `OSError` escapes `handle_stream_from_peer()`'s
  handshake-failure tolerance (contract §4) and tears down the
  **whole actor**. i.e. any connect-then-immediately-drop peer
  — a port scan, a liveness probe, a cancelled dial — is a
  remote actor-kill.

  Wrap both `getsockname`/`getpeername` in a tolerant helper
  and degrade to a port-id-less addr. A dead peer must cost us
  an addr, not the runtime.

  NOTE this is *not* hypothetical: the discovery suite's own
  `daemon` readiness probe
  (`tests/discovery/conftest.py`) does exactly this, which is
  how it was found.

---

## 4. Multiaddr representation

There is no `/tipc` in the multiaddr protocol table. Interim
grammar, mirroring how `uds` maps to the spec-legal `/unix`:

```
/tipc/<stype>/<instance>            # scope implied = cluster
/tipc/<stype>/<instance>/<scope>    # explicit
```

- `_tpt_proto_to_maddr['tipc'] = 'tipc'` and a `mk_maddr()`
  `case 'tipc':` building the above.
- `parse_maddr()` gets `case ['tipc']:` — but note
  `py-multiaddr` will reject an unregistered protocol name
  outright, so this **requires an upstream registration** (same
  track as the `wg` work, gh #483 /
  multiformats/py-multiaddr#107). Until that lands:
  - `MsgpackTIPCStream.maddr` returns the **`str`** form (the
    `MsgTransport.maddr` return type is already
    `Multiaddr|str`, and `MsgpackUDSStream.maddr` already
    exercises the `str` branch), and
  - `parse_maddr()` special-cases the `/tipc/` prefix *before*
    handing the string to `Multiaddr()`.
  Document this as the reason gh #443's "standardize on
  returning `Multiaddr` everywhere" item stays blocked.

Propose `/tipc/` upstream as: name `tipc`, code TBD, size
variable, value `<stype>:<instance>:<scope>` — or as three
composed protos. Prefer *one* proto with a structured value so
the maddr stays 2-segment like `/unix/...`.

---

## 5. Discovery: the actually-interesting part

Two independently-shippable layers. **Layer A is in scope for
the first PR; layer B is a fast-follow.**

### 5.1 Layer A — "discovery by bind" (free)

Because `bind(TIPC_ADDR_NAMESEQ)` publishes and
`connect(TIPC_ADDR_NAME)` resolves, a `tractor` tree whose
`registry_addrs` are TIPC service names needs **no registrar
liveness at all** for the connect path: `find_actor()`'s
"connect to the registrar and ask" becomes "connect to the
service name directly". Concretely:

- `tractor.discovery._api.find_actor()` etc. keep working
  unchanged (they go through the registrar), *and*
- a new, TIPC-only fast path becomes possible: derive an actor's
  service name from its `(name, uuid)` and dial it without any
  registrar hop.

Do **not** build the fast path in PR 1. Instead, prove the
property with a test (§7.4) and file the follow-up: it changes
`discovery` semantics (name→instance derivation must be a
documented, stable, cross-language-able hash) and deserves its
own design.

### 5.2 Layer B — the topology service (`TIPC_TOP_SRV`)

This is what makes #378's "end game cluster proto" claim real:
a *subscription* to name-table events, i.e. push-based
`register`/`deregister` for free, replacing the registrar's
polled `find_actor()`.

Mechanics (verify each field against
`linux/include/uapi/linux/tipc.h` + `net/tipc/topsrv.c` at
implementation time — the struct layout below is from the uapi
header and the byte-order caveat is real):

```python
# SOCK_SEQPACKET connected to the topology server
sock = trio.socket.socket(AF_TIPC, SOCK_SEQPACKET)
await sock.connect((
    socket.TIPC_ADDR_NAME,
    socket.TIPC_TOP_SRV,   # == 1
    socket.TIPC_TOP_SRV,
    0,
))

# struct tipc_subscr {
#   struct tipc_name_seq seq;  /* 3 * __u32: type, lower, upper */
#   __u32 timeout;             /* TIPC_WAIT_FOREVER == ~0 */
#   __u32 filter;              /* TIPC_SUB_{PORTS,SERVICE,CANCEL} */
#   char  usr_handle[8];
# }                            /* == 28 bytes */
_SUBSCR_FMT: str = '=IIIII8s'   # ⚠ 5*I is 20 -> use '=5I8s'
```

- **byte order**: the topology server historically accepts both
  host and swapped order and auto-detects; modern kernels are
  strict-ish. Pack native (`'='`) first, and if the server
  closes the connection immediately, retry with `'>'`. Encode
  that as a one-time probe helper
  `_detect_topsrv_endianness()` cached at module level — and
  put a `# ?TODO` pointing at `net/tipc/topsrv.c` for someone
  to make it deterministic.
- **events**: `struct tipc_event` is `event: u32`,
  `found_lower: u32`, `found_upper: u32`,
  `port: {ref: u32, node: u32}`, then the 28-byte subscription
  echo. `event ∈ {TIPC_PUBLISHED, TIPC_WITHDRAWN,
  TIPC_SUBSCR_TIMEOUT}`.

  ⚠️ **CORRECTION**: that totals **48** bytes
  (`4 + 4 + 4 + 8 + 28`), not the 40 an earlier revision of this
  plan claimed. Verified via `struct.calcsize()` at step 0. Use
  `'=5I8s'` (28) for the subscription and a 48-byte read for the
  event.

  ⚠️ also: python exposes `TIPC_WAIT_FOREVER` as **`-1`**, not
  `0xFFFFFFFF`, so it must be masked (`& 0xFFFFFFFF`) before
  packing into an unsigned `'I'` field.
- **trio shape** — this is where the "nearly-functional,
  modern-async" style pays off; expose it as an `@acm` yielding
  a `trio` receive-channel of typed events, *not* a class:

```python
@acm
async def open_topology_events(
    stype: int = TRACTOR_STYPE,
    lower: int = 0,
    upper: int = 0xFFFFFFFF,
    filter: int = TIPC_SUB_SERVICE,
    timeout: int = TIPC_WAIT_FOREVER,
    buf_size: int = 64,
) -> AsyncGenerator[
    trio.MemoryReceiveChannel[TIPCNameEvent],
    None,
]:
    ...
```

  with `TIPCNameEvent(msgspec.Struct, frozen=True)` fields
  `kind: Literal['published','withdrawn','timeout']`,
  `addr: TIPCAddress`, `node: int`, `ref: int`. One
  `trio.lowlevel`-free implementation: a nursery-spawned reader
  task doing `await sock.recv(48)` in a loop and
  `send_nowait()`ing decoded events, with the `@acm` closing the
  socket on exit → reader gets `ClosedResourceError` → cancel
  scope collapses. Standard `tractor` `@acm` discipline.
- **consumer**: `tractor/discovery/_registry.py` gains an
  optional "watch" mode so a registrar (or any actor) can keep
  a live view of the actor set without polling. Sketch the
  integration in the follow-up issue; do not wire it in PR 1.
- **`SOCK_SEQPACKET` is fine here** because this socket never
  goes through `MsgpackTransport` — it's a plain trio socket
  used with `recv()`. The contract's "`SOCK_STREAM` only"
  constraint applies to `MsgTransport` streams, not to this.

---

## 6. Commit sequencing (each independently reviewable + green)

**STATUS** (gh PR #493, stacked on #492): steps 1-5 landed as
9 commits, `22ef362d..51d7133f`. Acceptance bar met — 122
passed / 1 xfailed / 2 xpassed under `--tpt-proto tipc` across
`ipc`, `discovery`, `runtime`, `spawning`, `local`, `rpc`,
`cancellation`; `tcp`/`uds` unchanged. Steps 6-7 remain.

Two commits fell out that this plan did NOT anticipate,
- a wire-spec widening for the 4-tuple (§9), and
- an unrelated `devx.pformat` crasher that masked EVERY
  send-side `MsgTypeError`; it's on `main` and every branch,
  so it wants cherry-picking out of this stack.

1. `_server.py`: add `Address.rebind_from_sockname:
   ClassVar[bool]`, gate the `getsockname()` reconciliation on
   it, `True` for tcp/uds. Test: tcp `port=0` unchanged.
2. `tractor/ipc/_tipc.py`: `TIPCAddress` + `is_tipc_available()`
   predicate + `start_listener()`. No transport yet.
   Tests: address round-trip (`unwrap`/`from_addr`/`wrap_address`),
   `get_random()` uniqueness, bind/listen + `SO_ACCEPTCONN`
   tolerance, `EAFNOSUPPORT` → actionable `ConnectionError`.
3. `MsgpackTIPCStream` + `connect_to()` + `get_stream_addrs()`.
   Test: two `trio` tasks in one proc exchange a msg over
   `Msgpack` framing (no `tractor` runtime).
4. registration tables (contract §2 items 1-6, 9) +
   `pyproject.toml` mark/extra. Test: full suite under
   `--tpt-proto tipc` (§7.3).
5. maddr support (`str` form + prefix special-case) + docs.
6. `open_topology_events()` @acm + its tests (layer B).
7. docs page + `docs/` example.

Per project convention, a reproducing/guard test lands in its
own commit **before** the fix it guards.

---

## 7. Testing

### 7.1 the capability predicate (in `_tipc.py`, public)

```python
def is_tipc_available() -> bool:
    '''
    True iff this kernel can create an `AF_TIPC` socket, i.e.
    the `tipc` module is loaded.

    '''
    try:
        socket.socket(socket.AF_TIPC, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False
```

Cache it in a module global (it can't change without a
`modprobe`, and a cold call costs a syscall). Pure predicate, no
side effects, no logging.

### 7.2 gating

- `pytest.mark.tipc` registered in
  `_testing/pytest.py::pytest_configure()` alongside `no_tpt`,
  `skipon_spawn_backend` et al.
  ⚠️ **CORRECTION**: an earlier revision said `pyproject.toml`;
  the repo has no `[tool.pytest.ini_options] markers` table and
  registers every custom mark via `config.addinivalue_line()`.
  Per contract §0, the code wins.
- module-level
  `pytestmark = pytest.mark.skipif(not is_tipc_available(),
  reason='`tipc` kernel module not loaded (`modprobe tipc`)')`
  in `tests/ipc/test_tipc.py`.
- `--tpt-proto tipc` with no module must fail **loudly and
  early** with the actionable message, not with 400 confusing
  timeouts. Add the check to the `tpt_protos` fixture's existing
  per-proto validation loop (`_testing/pytest.py:795`): if the
  chosen `Address` type exposes an `is_available()`-style
  classmethod, call it and `pytest.fail()` with its reason.
  Generalize (don't special-case tipc) — plans 02/03 need the
  same hook.

### 7.3 CI

- add a job matrix entry `--tpt-proto tipc` that runs
  `sudo modprobe tipc` in a `before` step. GH's
  `ubuntu-latest` runners do allow `modprobe tipc` (the module
  ships with the standard Ubuntu kernel package); verify in a
  throwaway workflow before wiring the matrix. If it turns out
  to be unavailable, fall back to a container job with
  `--privileged`/`--cap-add NET_ADMIN`, and mark the job
  `continue-on-error` until it's proven stable.
- cross-node TIPC (bearer) cannot be CI'd; cover it with a
  documented manual smoke test in the docs page, in the style
  of gh #482's LAN examples.

### 7.4 backend-specific tests worth writing

- **name-publication is discovery**: bind a listener on
  `(stype, inst)`, then from a second task `connect()` by name
  and assert it lands — *without* any `tractor` registrar.
- **`get_random()` collision resistance**: 10k `get_random()`
  calls with no live runtime.
  ⚠️ **CORRECTION**: asserting **10k distinct** is a ~1.2%
  flaky test, not a guarantee —
  `P(collision) ≈ 1 - exp(-n²/2^33) ≈ 1.16e-2` for `n=10k` in a
  32-bit instance space. That's ~1-in-86 runs red, which the
  project's fix-flakes-at-source rule forbids. Assert
  `>= n - 2` instead (`P(>2 collisions) ≈ 1e-7`) and document
  the arithmetic inline.

  Also add a *deterministic* sibling asserting the derivation
  is a pure fn of the seed, which is the property the (§5.1)
  registrar-less fast path will actually depend on.

  ⚠️ do **NOT** take §9's "fold a 6-byte digest into
  `(stype_low, instance)`" escalation: §5.2's topology
  subscription can only watch **one** service type, so varying
  `_stype` per-actor would need 65536 subscriptions and kills
  layer B outright. The instance space is 32b and that's that;
  if crosstalk ever bites for real, the answer is the post-bind
  verification handshake, not stype bits.
- ✅ **SETTLED — round-robin surprise is REAL**: two listeners
  bound to the *same* `(stype, inst)` both bind fine and
  connects alternate strictly (`b,a,b,a,b,a` observed over 6
  dials). So a `get_random()` clash is *silent crosstalk*, never
  `EADDRINUSE`. Assert the observed behaviour and reference it
  from the `get_random()` docstring so the next reader knows
  why the hash matters.
- **scope isolation**: a `TIPC_NODE_SCOPE` bind is not visible
  to a cluster-scope lookup from another node (manual/marked).
- **importance opt** round-trips via `getsockopt`.
- **graceful + abrupt close** produce `TransportClosed` with the
  same `loglevel` classification as tcp/uds — i.e. re-run the
  relevant `tests/ipc/test_each_tpt.py` cases parametrized over
  the new proto rather than writing new ones.

---

## 8. Deployment / docs deliverable

A `docs/` page (and/or an `examples/` script) covering:

```bash
# single host, node-scope only
sudo modprobe tipc
tipc node get addr

# multi-host over ethernet (pairs beautifully with plan 03's wg)
sudo tipc bearer enable media eth device eth0
# ...or over UDP when L2 isn't available:
sudo tipc bearer enable media udp name uc localip 10.0.11.1
tipc link list
tipc nametable show          # <- see tractor's published services!
```

`tipc nametable show` displaying live `tractor` actors is the
single best demo this backend has; lead with it.

---

## 9. Known risks + escalations

Status column reconciled against the **step-0 probe on a live
kernel** (`modprobe tipc`, py3.13) plus the landed impl. Rows
marked ⚠️ are the ones whose *stated* mitigation turned out to
be wrong or insufficient.

| risk | status | mitigation |
| --- | --- | --- |
| `_instance` hash collision → silent crosstalk (two actors share a service name, TIPC round-robins connects between them) | ✅ **confirmed real** — dup binds both succeed, dials alternate strictly | `blake2b` 32b digest + §7.4 tests. ⚠️ the "6-byte digest folded into `(stype_low, instance)`" escalation is **withdrawn** — it breaks §5.2's single-type subscription. Real escalation is a post-bind verification handshake |
| kernel/module unavailability everywhere (dev boxes, macOS, CI) | ✅ handled | `is_tipc_available()` + the generic `Address.is_available() -> (ok, why_not)` hook consumed by the `tpt_protos` fixture; module stays importable on non-linux via uapi-value fallbacks. TIPC is *opt-in cluster* only, never a default |
| `getsockname()` returns port-id not name | ✅ confirmed (true even *pre*-bind) | `rebind_from_sockname` opt-out (§3.2), landed first |
| dial of an unpublished name doesn't normalize | ⚠️ **worse than stated** — `EHOSTUNREACH` is a **bare `OSError`**, not a `ConnectionError` subtype | `_reraise_as_connerr()` is REQUIRED for contract §4, not polish (§3.3) |
| a connect-then-drop peer kills the whole actor via `ENOTCONN` from `getpeername()` | ⚠️ **NOT in the original plan; found by our own test harness** | tolerant `getsockname`/`getpeername` helper degrading to a port-id-less addr (§3.4) |
| the unwrapped 4-tuple doesn't fit the wire msg-spec | ⚠️ **NOT in the original plan** — `SpawnSpec.reg_addrs`/`.bind_addrs` pinned a 2-tuple | widen to `UnwrappedAddress`, **variadic** `tuple[str\|int, ...]` since `msgspec` refuses a union w/ >1 array-like type. Own commit; first real bite of contract §1.1 |
| `SO_ACCEPTCONN` rejected by `AF_TIPC` | ✅ **non-issue** — answers `1` | none needed; pinned by a test anyway |
| unregistered `/tipc` multiaddr proto | ✅ handled (interim) | `str` maddr + `parse_maddr()` prefix special-case *before* `Multiaddr()` (§4); upstream track gh #483. Keeps gh #443 blocked |
| stale docs (#378 notes tipc.io docs may be out of date) | ✅ still true | treat `include/uapi/linux/tipc.h` + `net/tipc/` as the only normative source; cite file+symbol in code comments |
| `SOCK_SEQPACKET` topology framing byte-order | ⏳ open (layer B) | probe helper + `?TODO` (§5.2). Note the event struct is **48B not 40B** and `TIPC_WAIT_FOREVER` is `-1` in python |

Non-risks worth recording so nobody re-litigates them:

- **graceful peer close arrives as `BrokenResourceError`
  /`ECONNRESET`, not a clean 0-byte EOF** like tcp/uds. Benign:
  `MsgpackTransport._iter_packets()` already `match`es
  `'Connection reset by peer'` into the `loglevel='transport'`
  "normal operation breakage" branch, so `TransportClosed`
  classification is unchanged. Worth a sentence in the docs
  page (§8) since it *looks* alarming in transport logs.
- **`tipc nametable show` really does list our published
  services** (type `1953628160` == `0x74720000`), so the §8 demo
  works as advertised.

## 10. Follow-up issue seeds

- **register `/tipc` in the multiaddr spec**, mirroring the `wg`
  track (multiformats/py-multiaddr#107/#108 + gh #483). Same
  shape of work: propose the proto + code, land a codec in
  `py-multiaddr`, then drop our `str`-maddr fallback (§4). Worth
  filing *alongside* the `wg` spec-submission issue so both
  proposals go up together rather than as one-offs.
- registrar-less discovery fast path via name derivation (§5.1)
- `TIPC_TOP_SRV`-driven push registry in
  `discovery/_registry.py` (§5.2)
- `TIPC_IMPORTANCE` for the parent<->child lifetime channel
  (§3.3) — genuinely novel supervision QoS, no other backend
  can do it
- TIPC multicast / group messaging as a *broadcast* transport
  for `tractor.trionics` fan-out (explicitly not `MsgTransport`)
- dual-link resiliency / multi-homing (#378's "hybrid dual link")
  once bearers are scripted in the docs
