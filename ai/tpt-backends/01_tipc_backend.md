# Plan 01 — `TIPC` transport backend (`tractor/ipc/_tipc.py`)

Tracks gh [#378]. Prereq reading:
[`00_shared_backend_contract.md`](./00_shared_backend_contract.md).

**Thesis**: TIPC is the *cheapest* new backend we can add and
gives us kernel-native service-name publication, known-address
dialling, and topology events. Those are primitives for reducing
registrar traffic; they do **not** by themselves replace
`tractor.discovery`, derive an actor's address from its name, or
elect one registrar. It is stdlib-only: zero new dependencies.

This plan is reconciled against downstream PR [#493]'s code and
tests. Treat that implementation as prior art without mistaking
implemented transport primitives for completed discovery policy.

[#378]: https://github.com/goodboy/tractor/issues/378
[#493]: https://github.com/goodboy/tractor/pull/493

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

This is the "leverage the built-in discovery machinery" part of
#378: publishing a bind is kernel name-table registration and
`connect()` on an already-known name is a kernel lookup, with no
registrar actor on that **dial** path. Mapping an application name
to that address and maintaining Tractor's actor registry remain
separate work (§5).

### 2.2 the struct

```python
class TIPCAddress(
    msgspec.Struct,
    frozen=True,
):
    _stype: int                    # TIPC "type" == service class
    _instance: int                 # service instance within the type
    _scope: int = TIPC_CLUSTER_SCOPE
    # observed-only, excluded from the unwrapped service identity
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

> **make the unwrapped form carry the explicit internal
> `TransportProtocolKey`.**

```python
def unwrap(self) -> tuple[str, int, int, int]:
    return ('tipc', self._stype, self._instance, self._scope)
```

`wrap_address()` then dispatches `_address_types[addr[0]]` and
the collision class disappears. The complete all-backend change
is a prerequisite migration; #493 necessarily carried the
transitional `UnwrappedAddress`/`SpawnSpec.reg_addrs`/
`.bind_addrs` widening needed for TIPC. See contract §1.1 for the
remaining runtime annotations, fixtures and `piker` config. Here
`tipc` is both the internal and external spelling; UDS remains
internally `uds` and translates explicitly to external `/unix/`.

`msgpack` decodes tuples as lists, so both forms are part of the
round-trip contract. Match only the exact three- or four-element
tagged shapes and test all four routes:

```python
case (
    ('tipc', int() as stype, int() as inst, int() as scope)
    |
    ['tipc', int() as stype, int() as inst, int() as scope]
):
    ...
```

Also test the scope-defaulted three-element form through
`TIPCAddress.from_addr()`, and tuple/list forms through the global
`wrap_address()`. A normal two-element TCP/UDS address whose first
element happens to be `'tipc'` must retain its classic dispatch.

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
  reproducible and well-distributed, **not collision-free**:
  ```python
  # 32-bit instance derived from the actor's Aid.uid, or from a
  # per-call token + pid when there is no live runtime.
  inst: int = int.from_bytes(
      blake2b(seed.encode(), digest_size=4).digest(),
      'big',
  )
  ```
  where `seed = '.'.join(actor.aid.uid)` if
  `current_actor(err_on_no_runtime=False)` else
  `f'{prefix}.{uuid4().hex[:8]}@{pid}'`. Must avoid the reserved
  low range: `inst = 64 + (inst % (2**32 - 64))`.
  The UUID is load-bearing because TIPC names are cluster-wide
  while PIDs are only host-local: `(actor name, pid)` can alias on
  different hosts.
  ⚠️ *unlike* `port=0`, a collision here surfaces as a
  successful-but-shared publication (TIPC allows multiple
  binders on the same name and round-robins!) rather than
  `EADDRINUSE`. That is a silent-crosstalk failure mode; §7 has
  a statistical test and §9 records the unresolved recovery work
  in [#501].
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
        self._instance > 0
        and
        self._stype > 0
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
- `SocketListener.__init__` calls
  `getsockopt(SOL_SOCKET, SO_ACCEPTCONN)`. The live-kernel probe
  used by #493 answers `1`; retain the unit test so a kernel-side
  change is visible rather than relying on trio's suppressed-
  `OSError` carve-out.
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

`TIPCAddress.from_addr()` must accept only proto-keyed service
names. It must reject a bare port ID because no conversion can
recover `(stype, instance)`:

```python
@classmethod
def from_addr(cls, addr) -> TIPCAddress:
    match addr:
        # our proto-keyed tuple or decoded-list wire form
        case (
            ('tipc', int() as stype, int() as inst, int() as scope)
            |
            ['tipc', int() as stype, int() as inst, int() as scope]
        ):
            return TIPCAddress(stype, inst, _norm_scope(scope))

        # a bare kernel-observed TIPC_ADDR_ID 5-tuple has no
        # service identity to annotate.
        case (int() as atype, *rest) if atype == socket.TIPC_ADDR_ID:
            raise ValueError(...)
```

The `TIPC_ADDR_ID` case cannot reconstruct `(stype, instance)`.
The resolution is the explicit listener-rebind policy added ahead
of the backend in #493:

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
hack. Keep the guard test that TCP's `port=0` behaviour is
unchanged.

Do **not** annotate `Endpoint.addr` from `getsockname()`: the
listener endpoint must remain the dialable service name. Port IDs
are observed only on connected streams and may annotate a copy via
`with_port_id()` purely for logging/repr.

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
            stream = trio.SocketStream(sock)
            return cls(
                stream,
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
- **`connect_to()` on a name with no publisher**: the live-kernel
  result is immediate `EHOSTUNREACH`. Python exposes that as a
  bare `OSError`, not a `ConnectionError` subtype, so
  `_reraise_as_connerr()` is load-bearing for contract §4. Keep
  the exact errno and normalization under test.

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

- `get_stream_addrs()` converts both socket results into
  **observed-only** addresses: `_stype`/`_instance` use the
  `TIPC_NAME_UNKNOWN = -1` sentinel and `maybe_node`/`maybe_ref`
  carry the port ID. Such addresses are invalid for dialling.
- the **connecting** side knows the service name it dialled, so
  `connect_to()` replaces `_raddr` after construction with that
  known `TIPCAddress` while retaining the constructor's one
  tolerant port-ID observation. Do not call `getpeername()` a
  second time: the peer can withdraw between the two calls.
- the **accepting** side genuinely cannot recover the peer's
  service name from a port ID. Keep the observed-only `raddr`;
  the handshake's `Aid` supplies logical identity. Piggybacking a
  bound name in the handshake is outside this backend.
- `laddr` is observed-only as well. It is used for repr/logging,
  not to replace the endpoint's known service name.
- unlike TCP/UDS, TIPC can answer `ENOTCONN` from
  `getpeername()` after a connect-then-drop. This lookup happens
  during `MsgpackTransport` construction, before handshake error
  tolerance. Wrap `getsockname()` and `getpeername()` in a
  tolerant helper and degrade to a port-ID-less observed address;
  a dropped peer must cost an observation, not kill the actor.

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

## 5. Discovery primitives and explicit limits

The backend provides independently-shippable kernel primitives.
Neither primitive alone implements Tractor's actor-name discovery,
registry ownership, or registrar election.

### 5.1 Layer A — "discovery by bind" (free)

Because `bind(TIPC_ADDR_NAMESEQ)` publishes and
`connect(TIPC_ADDR_NAME)` resolves, a caller that **already knows**
a TIPC service address can dial it without a registrar lookup.
This is narrower than registrar-less `find_actor(name)`:

- `tractor.discovery._api.find_actor()` and peers still query a
  registrar; #493 does not change them.
- deriving a stable service address from `(name, uuid)` and
  dialling it directly is follow-up [#499]. The mapping must be
  documented and cross-language stable.
- `registry_addrs` still identify registrars. Connecting to a
  known registrar by TIPC name removes no registrar bookkeeping
  or ownership semantics.

There is also an unresolved **split-brain election** problem.
Duplicate TIPC name publication succeeds and round-robins, so two
roots can both probe an unoccupied registrar name, both bind it,
and both believe they won. The backend provides no atomic
compare-and-publish, lease, quorum, or deterministic winner. A
topology subscription can reveal multiple publisher port IDs but
does not elect or fence one. Do not describe registrar election as
solved until a separate protocol closes this race.

### 5.2 Layer B — the topology service (`TIPC_TOP_SRV`)

This is the push primitive behind #378's "end game cluster proto"
direction: a subscription to kernel name-table publish/withdraw
events. #493 implements `open_topology_events()`; consuming that
feed in `discovery._registry` is follow-up [#496]. Until then it
does not replace registrar state or `find_actor()`.

Mechanics, verified against `linux/include/uapi/linux/tipc.h`,
`net/tipc/topsrv.c` and #493's live-kernel probe:

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
_SUBSCR_FMT: str = '=5I8s'
```

- **byte order**: #493's live-kernel probe verified native
  standard-size (`'='`) packing for publish and withdraw events.
  Use `'=5I8s'` for the 28-byte subscription. Do not retain the
  speculative `'>'` retry/probe as if it were required. Preserve
  the earlier `# ?TODO` to verify the deterministic rule directly
  against `net/tipc/topsrv.c`; it is source-audit work, not a
  runtime retry requirement.
- **events**: `struct tipc_event` is `event: u32`,
  `found_lower: u32`, `found_upper: u32`,
  `port: {ref: u32, node: u32}`, then the 28-byte subscription
  echo: **48 bytes** (`4 + 4 + 4 + 8 + 28`), not 40. Use
  `'=10I8s'` and assert `struct.calcsize(...) == 48`.
  `event ∈ {TIPC_PUBLISHED, TIPC_WITHDRAWN,
  TIPC_SUBSCR_TIMEOUT}`. Python exposes `TIPC_WAIT_FOREVER` as
  `-1`, so mask it with `& 0xFFFF_FFFF` before packing an
  unsigned `I`.
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
  task doing `await sock.recv(48)` in a loop. The feed is
  authoritative and may neither block the socket reader nor drop
  transitions silently. Use `send_nowait()` and, on
  `trio.WouldBlock`, raise a dedicated
  `TIPCNameEventOverflow` that aborts the subscription and tells
  the consumer to resubscribe and rebuild its view. A timeout
  event is delivered once and then closes the channel. The
  `@acm` cancels its reader before closing the fd so teardown
  cannot race a retried `recv()` into `EBADF`.
- **scope**: topology events carry no publication scope. Use an
  explicit unknown-scope sentinel and keep the resulting address
  non-dialable; never copy caller/subscription context into
  supposedly observed data.
- **consumer**: [#496] owns the optional watch mode and the
  decision whether the feed subsumes or merely accelerates
  existing registrar bookkeeping.
- **`SOCK_SEQPACKET` is fine here** because this socket never
  goes through `MsgpackTransport` — it's a plain trio socket
  used with `recv()`. The contract's "`SOCK_STREAM` only"
  constraint applies to `MsgTransport` streams, not to this.

---

## 6. Commit sequencing (each independently reviewable + green)

1. `_server.py`: add `Address.rebind_from_sockname:
   ClassVar[bool]`, gate the `getsockname()` reconciliation on
   it, `True` for tcp/uds. Test: tcp `port=0` unchanged.
2. `tractor/ipc/_tipc.py`: `TIPCAddress` + `is_tipc_available()`
   predicate + `start_listener()`. No transport yet.
   Tests: address round-trip (`unwrap`/`from_addr`/`wrap_address`),
   `get_random()` distribution, bind/listen + `SO_ACCEPTCONN`
   tolerance, `EAFNOSUPPORT` → actionable `ConnectionError`.
3. `MsgpackTIPCStream` + `connect_to()` + `get_stream_addrs()`.
   Test: two `trio` tasks in one proc exchange a msg over
   `Msgpack` framing (no `tractor` runtime).
4. registration tables (contract §2 items 1-8 and 10) +
   `pyproject.toml` mark/extra. Test: full suite under
   `--tpt-proto tipc` (§7.3). Keep TIPC in the conservative remote
   preference tier until a follow-up implements and tests contract
   item 9's node-scope locality policy.
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
    if sys.platform != 'linux':
        return False
    try:
        socket.socket(socket.AF_TIPC, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False
```

Do not permanently memoize the result: `modprobe tipc` and module
removal can change it during a long-lived process. Probe once per
runtime startup, or use an explicitly refreshable cache whose
owner invalidates it after module-management operations. The
predicate itself remains side-effect-free and silent.

### 7.2 gating

- `pytest.mark.tipc` registered in
  `_testing/pytest.py::pytest_configure()` via
  `config.addinivalue_line()`, where this repo declares its other
  custom marks. Do not invent a `pyproject.toml` marker table.
- keep pure address, serialization, and topology-codec tests
  runnable on every host. Apply a shared `requires_tipc` marker
  only to tests that create sockets or otherwise touch the kernel;
  do not module-skip `tests/ipc/test_tipc.py`.
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
  throwaway workflow before wiring the matrix. #493's TIPC leg
  is now blocking. If runners cease permitting the module load,
  fix the environment or use a suitable container rather than
  silently restoring `continue-on-error`.
- cross-node TIPC (bearer) cannot be CI'd; cover it with a
  documented manual smoke test in the docs page, in the style
  of gh #482's LAN examples.

### 7.4 backend-specific tests worth writing

- **known-name publication/resolution**: bind a listener on
  `(stype, inst)`, then from a second task `connect()` by name
  and assert it lands — *without* any `tractor` registrar.
- **`get_random()` distribution**: 10k `get_random()` calls with
  no live runtime. Do **not** assert 10k distinct values: the
  no-runtime seeds and outputs are both only 32 bits. Including
  duplicate seeds plus distinct-seed hash collisions puts the
  modeled chance of at least one duplicate near 2.3% for 10k
  calls. #493 uses `>= n - 2` (modeled probability of more than
  two collisions around `2e-6`) and separately proves
  `instance_from_seed()` is a pure function. Also hold actor
  name/PID fixed while varying only `Aid.uuid` to prove live
  actors seed from `Aid.uid`.
- **round-robin surprise**: two listeners bound to the *same*
  `(stype, inst)` both succeed (TIPC allows it) and connects
  distribute. Assert the observed behaviour and reference it
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

- **Instance collision / silent crosstalk remains unresolved.**
  `Aid.uid` seeding and §7.4 tests reduce and measure risk, but
  the instance field is still a hard 32 bits. [#501] owns
  post-bind verification and recovery. Do not fold bits into
  `_stype`: topology can watch only one service type.
- **Concurrent registrar startup can split brain.** Topology can
  observe duplicate publisher port IDs but cannot elect or fence
  a winner; a separate election protocol is required (§5.1).
- **Kernel/module availability is opt-in.** Keep the hard gate in
  §7.2; TIPC is never the default transport.
- **A listener sockname is a port ID, not its service name.** Keep
  the `rebind_from_sockname` opt-out (§3.2).
- **`/tipc` is not yet a registered multiaddr protocol.** Keep
  the interim `str` maddr fallback (§4) and upstream gh #483.
- **The public TIPC docs can be stale.** Treat
  `include/uapi/linux/tipc.h` and `net/tipc/` as normative and
  cite file/symbol names in code comments.
- **A slow topology consumer loses continuity.** Fail fast with
  `TIPCNameEventOverflow`; resubscribe and rebuild rather than
  block the reader or retain stale state (§5.2).
- **TIPC locality preference is not implemented.** Current
  `_is_local_addr()` handles only UDS and TCP, so node- and
  cluster-scope TIPC both remain in the conservative remote tier.
  Add explicit scope-aware policy and multihomed selection tests
  before claiming node-scope preference (contract §2.9).

### 9.1 remaining constructor/error cleanup

#493 closes the peer-withdrawal race in transport construction,
but it is not a blanket error-path cleanup. Keep these gaps
explicit rather than reporting the backend as fully hardened:

- direct `TIPCAddress(...)` construction bypasses
  `from_addr()` scope normalization; `is_valid` is queried later
  rather than enforcing validity at construction. Decide whether
  constructors should reject bad service types/instances/scopes
  or document direct construction as trusted-internal.
- `maybe_node`/`maybe_ref` are excluded from `.unwrap()` but, as
  `msgspec.Struct` fields, still participate in structural
  equality/hash. If service-name identity must ignore observation
  metadata, represent or compare it explicitly instead of relying
  on the current "observed-only" description.
- `start_listener()` must keep ownership of the raw socket through
  `bind()`, `listen()` and `SocketListener(...)`. The downstream
  implementation normalizes bind errors but does not yet wrap the
  complete listener-construction sequence in close-on-error, so a
  later setup failure can leak the fd.
- `_maybe_sockaddr()` currently degrades every `OSError` to an
  unknown observed address. Narrow that tolerance to expected
  peer-withdrawal errors (notably `ENOTCONN`) so unrelated bad-fd
  or programming failures remain visible.
- error normalization is intentionally required for an
  unpublished-name `EHOSTUNREACH`, but setup `setsockopt`,
  listener-constructor, and topology setup failures still need a
  consistent policy and focused regression tests.

## 10. Follow-up issue seeds

- **register `/tipc` in the multiaddr spec**, mirroring the `wg`
  track (multiformats/py-multiaddr#107/#108 + gh #483). Same
  shape of work: propose the proto + code, land a codec in
  `py-multiaddr`, then drop our `str`-maddr fallback (§4). Worth
  filing *alongside* the `wg` spec-submission issue so both
  proposals go up together rather than as one-offs.
- registrar-less discovery fast path via name derivation ([#499],
  §5.1)
- `TIPC_TOP_SRV`-driven push registry in
  `discovery/_registry.py` ([#496], §5.2)
- post-bind collision verification and recovery ([#501], §9)
- `TIPC_IMPORTANCE` for the parent<->child lifetime channel
  (§3.3) — genuinely novel supervision QoS, no other backend
  can do it
- TIPC multicast / group messaging as a *broadcast* transport
  for `tractor.trionics` fan-out (explicitly not `MsgTransport`)
- dual-link resiliency / multi-homing (#378's "hybrid dual link")
  once bearers are scripted in the docs

[#496]: https://github.com/goodboy/tractor/issues/496
[#499]: https://github.com/goodboy/tractor/issues/499
[#501]: https://github.com/goodboy/tractor/issues/501
