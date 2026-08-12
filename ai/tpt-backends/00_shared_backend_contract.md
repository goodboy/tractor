# `tractor.ipc` next-gen transport backends: the shared contract

Status: design doc / implementation spec.
Audience: any model or human implementing one of the three
sibling plans in this directory.

- [`01_tipc_backend.md`](./01_tipc_backend.md) — `AF_TIPC`
  (gh #378)
- [`02_quic_iroh_backend.md`](./02_quic_iroh_backend.md) — QUIC
  via `iroh` FFI, uniffi-async rewritten onto `trio` (gh #353)
- [`03_wg_tunnel_bindspace.md`](./03_wg_tunnel_bindspace.md) —
  WireGuard (and other shuttle-able) tunnels as a *nested
  bindspace* layer via `pyroute2` (gh #482, #443)

This doc is the **normative** description of what a `tractor`
transport backend *is* as of `main@83b34884`. Each sibling plan
assumes it and only documents its own deltas. Read this first;
do not re-derive it from the code.

---

## 0. Why a shared contract doc

The three plans are meant to be implementable *independently and
concurrently* by different models/providers without design
drift. Everything they share — the backend duck-type, the
registration tables, the test harness plumbing, the naming and
code-style rules — lives here exactly once. If an implementer
finds this doc disagrees with `main`, **the code wins**; fix this
doc in the same PR.

---

## 1. The backend duck-type (empirical, from `_tcp.py`/`_uds.py`)

A transport backend is **one module** under `tractor/ipc/`
exposing exactly four things. There is no ABC to subclass and no
plugin entrypoint; wiring is by explicit table registration
(§2) plus one piece of reflection (§1.3).

### 1.1 `class <Proto>Address(msgspec.Struct, frozen=True)`

Structurally conforms to the `Address` `Protocol` in
`tractor/discovery/_addr.py:82`. Required surface:

| member | kind | notes |
| --- | --- | --- |
| `proto_key` | `ClassVar[str]` | the wire/registry key, e.g. `'tcp'`, `'uds'` |
| `unwrapped_type` | `ClassVar[type]` | the primitive tuple shape |
| `def_bindspace` | `ClassVar` | default bindspace value |
| `is_valid` | `@property -> bool` | "is this a *dialable/bindable* addr" |
| `bindspace` | `@property` | the "set of hosts"-ish scope (see below) |
| `from_addr(cls, addr)` | `@classmethod` | primitive -> wrapped, `match`-based |
| `unwrap(self)` | method | wrapped -> primitive (must be msgpack-native!) |
| `get_random(cls, bindspace=...)` | `@classmethod` | per-subactor ephemeral addr |
| `get_root(cls)` | `@classmethod` | host-singleton default registrar addr |
| `__repr__` | method | `f'{type(self).__name__}[{...}]'` house style |

Hard constraints learned from the existing two:

- **`frozen=True`.** Addresses are dict keys
  (`Server.epsdict()`, `Endpoint.peer_tpts`) and are compared by
  value all over the runtime.
- **`.unwrap()` output must round-trip through `msgspec` and
  through `wrap_address()`.** It is what actually crosses the
  wire in `SpawnSpec`/`_root_mailbox`/`_registry_addrs`, and it
  is what `Actor.reg_addrs` and every test compares against. If
  your unwrapped form is not *uniquely* pattern-matchable
  against the other backends' forms in
  `wrap_address()` (`_addr.py:230`), you have a bug that
  manifests as the wrong transport being loaded — the file's own
  `XXX NOTE` warns about precisely this.
- **`.get_random()` must be collision-free without a live
  runtime.** See the `UDSAddress.get_random()` uuid-token
  comment (`_uds.py:207-220`): with no `current_actor()` the
  sockname degenerates to a pure fn of `(prefix, pid)` and two
  calls in one proc alias. Mix in a `uuid4().hex[:8]` token.
- **`.bindspace` semantics**: "the address' bindable space" —
  ip/host for `tcp`, the socket-file *directory* for `uds`. For
  the new backends: the TIPC *scope* (§1 of plan 01), the iroh
  *ALPN + relay/discovery realm* (plan 02), the netns (plan 03).
  `Address.namespace` is already spec'd in the Protocol as
  "the if-available OS-specific network namespace key" and is
  currently unimplemented by both backends — plan 03 is the
  first real consumer.

### 1.2 module-level listener lifecycle

```python
async def start_listener(
    addr: <Proto>Address,
    **kwargs,
) -> trio.SocketListener   # or a trio.abc.Listener, see §3
    ...

def close_listener(          # OPTIONAL
    addr: <Proto>Address,
    lstnr: trio.abc.Listener,
) -> None:
    ...
```

`close_listener()` is optional; `Endpoint.close_listener()`
(`_server.py:674`) `getattr`s it and treats absence as "closing
is implicit". `uds` needs it (unlinks the sock-file), `tcp`
does not.

### 1.3 the ONE piece of reflection you must not break

`Endpoint.start_listener()` (`_server.py:656`):

```python
tpt_mod: ModuleType = inspect.getmodule(self.addr)
lstnr = await tpt_mod.start_listener(addr=self.addr)
```

The transport module is found by `inspect.getmodule()` **on the
`Address` instance**. Therefore: *the `Address` class and its
`start_listener()`/`close_listener()` MUST live in the same
module.* Do not define the address type in `_types.py` or a
`_addrs.py` and the listener elsewhere.

Immediately after, the same method does:

```python
if (unwrapped := lstnr.socket.getsockname()) != self.addr.unwrap():
    self.addr = self.addr.from_addr(unwrapped)
```

i.e. it assumes `lstnr.socket.getsockname()` exists and that its
return value is a valid `from_addr()` input. This is fine for
TIPC (§3 of plan 01) and **is the main integration hazard for
iroh** (§3 of plan 02) — plans that break it must say so
explicitly and propose the upstream `_server.py` patch.

### 1.4 `class Msgpack<Proto>Stream(MsgpackTransport)`

Subclass `tractor.ipc._transport.MsgpackTransport`. You inherit
all framing (`<I` 4-byte little-endian length prefix),
`msgspec` codec ctx-var lookup, `TransportClosed` normalization,
`.drain()`, `__aiter__`. You implement only:

| member | notes |
| --- | --- |
| `address_type` | the `<Proto>Address` class |
| `layer_key: int` | OSI-ish layer, `4` for both current backends |
| `maddr` `@property` | `-> Multiaddr\|str`, via `mk_maddr(self.raddr)` |
| `connected(self) -> bool` | `tcp`/`uds` both use `self.stream.socket.fileno() != -1` |
| `connect_to(cls, addr, prefix_size=4, codec=None, **kw)` | `@classmethod`, returns an instance |
| `get_stream_addrs(cls, stream) -> (laddr, raddr)` | `@classmethod`, called from `MsgpackTransport.__init__` |

`MsgpackTransport.__init__` requires the object passed as
`stream` to satisfy:

- `await stream.send_all(bytes)`
- usable as `tricycle.BufferedReceiveStream(transport_stream=stream)`,
  i.e. `await stream.receive_some(n)`
- `trio.BrokenResourceError` / `trio.ClosedResourceError` /
  `ValueError('...unclean EOF...')` on the failure paths that
  `_iter_packets()` and `send()` already `match` on
  (`_transport.py:221-304`, `:436-499`).

That is **`trio.abc.Stream`, not `trio.SocketStream`**. The
`MsgTransport` Protocol's `stream: trio.SocketStream`
annotation (`_transport.py:83`) is a lie of convenience — the
actual `MsgpackTransport.__init__` param is typed
`trio.abc.Stream` and nothing in the msg path touches
`.socket`. Only `connected()` (which each backend defines) and
`Endpoint.start_listener()`'s `getsockname()` do.

### 1.5 verified-good news for socket-family backends

Both `trio.SocketStream` and `trio.SocketListener` are
**address-family agnostic**. Verified against the installed
`trio` (`trio/_highlevel_socket.py`): the only constructor
checks are

- `isinstance(socket, trio.socket.SocketType)`
- `socket.type == SOCK_STREAM`
- (listener) `getsockopt(SOL_SOCKET, SO_ACCEPTCONN)` is truthy,
  with `OSError` **suppressed** (the macOS carve-out, which
  also covers exotic families that reject the opt)

There is no `AF_*` check and no `IPPROTO_TCP` hard dependency
(`TCP_NODELAY`/`TCP_NOTSENT_LOWAT` are set under
`suppress(OSError)`). Consequence: **any `SOCK_STREAM` family
CPython can create — including `AF_TIPC` — drops straight into
the existing `trio.SocketStream` + `trio.serve_listeners()`
path.** This is why plan 01 is small and plan 02 is not.

---

## 2. Registration tables (the full wiring checklist)

Adding a backend touches these and only these:

1. `tractor/runtime/_state.py:46`
   `TransportProtocolKey = Literal['tcp', 'uds', ...]` — add the
   key. This `Literal` is the canonical set; `_testing/pytest.py`
   drives `--tpt-proto` validation off `_addr._address_types`,
   and the spawn-backend fixture already models the
   "drive-the-set-from-the-Literal" pattern
   (`pytest.py:870-880`) — do the same rather than hardcoding.
2. `tractor/discovery/_addr.py:173` `_address_types: bidict` —
   `{'<key>': <Proto>Address}`. Note it is a **`bidict`**, so
   the mapping must stay 1:1.
3. `tractor/discovery/_addr.py:181` `_default_lo_addrs` —
   `'<key>': <Proto>Address.get_root().unwrap()`.
   ⚠️ this dict is built at **import time**, so
   `get_root()` must not require a live runtime, a loaded kernel
   module, or network I/O. (`UDSAddress.def_bindspace =
   get_rt_dir()` is the precedent for "cheap, pure, filesystem-
   ish".) A backend whose root addr needs I/O must make this
   entry lazy — propose that refactor explicitly.
4. `tractor/discovery/_addr.py:230` `wrap_address()` `match` —
   add a case iff your `unwrapped_type` isn't already uniquely
   matched. Prefer unwrapped forms that are *self-tagging*
   (see plan 01 §2.2 and plan 02 §2.2) so this stays cheap.
5. `tractor/ipc/_types.py` — `Address` union alias,
   `_msg_transports` list, `_key_to_transport[('msgpack', key)]`,
   `_addr_to_transport[<Proto>Address]`.
6. `tractor/ipc/_types.py:92` `transport_from_stream()` — the
   `sock.family` `match`. For a non-socket stream type (iroh)
   this needs a different discriminator; see plan 02 §3.3.
7. `tractor/discovery/_multiaddr.py` —
   `_tpt_proto_to_maddr`, and a `case` in both `mk_maddr()` and
   `parse_maddr()`.
8. `tractor/ipc/__init__.py` — re-export if the backend has a
   public surface.
9. `tractor/_testing/addr.py::get_rando_addr()` — per-proto
   branch so the whole suite can run under `--tpt-proto <key>`.
10. `pyproject.toml` — new deps go in an **optional extra**, never
    in `[project].dependencies`. See §5.

## 3. Where the `trio.SocketListener` assumption is load-bearing

`_serve_ipc_eps()` (`_server.py:1041`) annotates
`listener: trio.abc.Listener` and hands the list to
`trio.serve_listeners(handler=handle_stream_from_peer,
listeners=..., handler_nursery=stream_handler_tn)`.
`trio.serve_listeners` itself is generic over
`trio.abc.Listener`. So the *only* `SocketListener`-specific
code in the server path is the `getsockname()` reconciliation in
`Endpoint.start_listener()` (§1.3) and the type annotations.

`handle_stream_from_peer()` (`_server.py:298`) then does
`Channel.from_stream(stream)` →
`transport_from_stream(stream)` → `sock.family` match (§2.6).

**Therefore**: a non-socket backend needs (a) a
`trio.abc.Listener` subclass, (b) a change to
`Endpoint.start_listener()` to not blindly `getsockname()`, and
(c) a change to `transport_from_stream()`'s discrimination.
All three are small, upstream-able, and *should be landed as
their own prep PR* before the backend itself — see plan 02 §3.

## 4. Handshake / discovery invariants you inherit

- Every accepted stream immediately does
  `chan._do_handshake(aid=actor.aid)`; a peer that fails it is
  logged at `runtime` and dropped, **not** raised
  (`_server.py:334-365`). Discovery-sys "pings" rely on this,
  so your `connect_to()` must raise something that normalizes
  to `TransportClosed`/`ConnectionError` on a dead peer, never
  a novel exception type.
- `_root.py:381-406` fail-fasts when a `registry_addrs` entry's
  `proto_key` is not in `enable_transports`. Your key must be
  spellable in both.
- `_root.py:256` currently enforces `len(enable_transports) == 1`.
  Multi-tpt actors are a separate work item; none of these three
  plans may depend on lifting it.
- Sub-actor bind addrs come from
  `_runtime.py:1600-1610`: for each key in the parent-supplied
  `enable_transports`, `get_address_cls(key).get_random()`.
  So `get_random()` runs *in the child, post-fork, pre-listen*.
  Anything it needs (kernel module, netns membership, an iroh
  secret key) must already be true at that moment.

## 5. Dependency policy

`[project].dependencies` stays lean (see the boot-latency work,
gh #470: `import tractor` is budgeted at ~0.145s). Every new
backend dep is an extra:

```toml
[project.optional-dependencies]
tipc = []                       # stdlib-only!
quic = ["iroh>=0.35"]           # pin per plan 02 §1
wg   = ["pyroute2>=0.9"]        # pin per plan 03 §1
```

and every backend module must be **import-lazy**: a
`tractor/ipc/_<proto>.py` that imports its 3rd-party dep at
module scope must not be imported by `tractor/__init__.py`,
`tractor/ipc/__init__.py`, or `tractor/discovery/_addr.py`'s
import-time table construction. The `_addr._default_lo_addrs`
eager-dict (§2.3) is the trap: keep the backend's `get_root()`
dep-free, or make that table lazy.

## 6. Test-harness plumbing (identical for all three)

- `--tpt-proto <key>` (`_testing/pytest.py:409`) selects the
  session-wide proto; the `tpt_proto` fixture mutates
  `_state._def_tpt_proto` + `_runtime_vars['_enable_tpts']`
  (`pytest.py:807-835`). Adding the key to `_address_types` is
  what makes `--tpt-proto <key>` legal (`pytest.py:795-800`
  asserts the lookup).
- The **acceptance bar** for every backend is: the *entire*
  existing suite passes under `--tpt-proto <key>`, unmodified.
  That is the whole point of the abstraction. Backend-specific
  unit tests go in `tests/ipc/test_each_tpt.py` (the existing
  `test_uds_bindspace_created_implicitly` /
  `test_uds_double_listen_raises_connerr` are the model).
- Capability gating: each backend needs a **cheap, pure
  predicate** + a `pytest.mark.skipif`, because these are all
  environment-dependent. Verified example: on this dev box
  `socket.socket(AF_TIPC, SOCK_STREAM)` raises
  `OSError(97, 'Address family not supported by protocol')`
  because the `tipc` module isn't loaded. Put the predicate in
  the backend module (so apps can use it too), not in the test.
- New pytest marks must be registered in `pyproject.toml`, per
  the project's fix-warnings-at-source rule (gh #469).

## 7. Code style (non-negotiable, matches the repo)

- module header tagline: `# tractor: distributed structured
  concurrency.` for **new** files (not the legacy
  `structured concurrent "actors".` form the existing `_tcp.py`
  carries).
- AGPL header block copied verbatim from `_tcp.py`.
- `from __future__ import annotations` first.
- annotate *everything*, including locals:
  `sockpath: Path = addr.sockpath`.
- `match`/`case` over `isinstance` chains for address and
  error dispatch.
- multi-line call/`import` style with trailing commas.
- never emit a whitespace-only line.
- error messages are multi-line f-strings ending in `\n`, with
  the `f'...\n' f'...\n'` implicit-concat layout and the
  `>[`/`[>`/`<=(` nested-op sigils where a `nest_from_op()` is
  in play.
- prefer pure functions + module-level helpers over methods;
  keep `Address` types data-only. Where a helper needs
  scoped setup/teardown, it's an `@acm` — not a class with
  `.start()`/`.stop()`.
- pure getters: no `get_*(..., mutate=True)` flags; split into
  a read-only getter and an explicit sibling setter.

---

## 8. Cross-plan sequencing

The three are independent *except*:

- plan 02 (iroh) needs the `_server.py` /
  `transport_from_stream()` generalization (§3) — plan 01 does
  **not**, and should therefore land first as the cheap proof
  that the table-registration story works for a genuinely new
  proto.
- plan 03 (wg) composes *under* whatever L4 tpt is in use and
  its netns work is what finally implements
  `Address.namespace`. It can land before or after 02, but its
  `TunnelledAddress` design must be reviewed against plan 02's
  address shape so the "tunnelled maddr" grammar (gh #443)
  covers `/…/quic-v1/p2p/…` inner addrs too.
- All three want first-class `wg`/`quic`/`tipc` protos in
  `py-multiaddr`; that upstream track is gh #483 and
  multiformats/py-multiaddr#107/#108.
