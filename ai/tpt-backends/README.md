# next-gen `tractor.ipc` transport backend plans

Implementation specs for three prospective `.ipc` transport
backends, written so each can be worked independently (by a
different model/provider) without design or lib-selection drift.

**Read [`00_shared_backend_contract.md`](./00_shared_backend_contract.md)
first** — it is the normative description of what a `tractor`
transport backend *is* as of `main@83b34884` (the backend
duck-type, registration and address-selection wiring, the
test-harness plumbing, the code-style rules). The three plans
assume it and document only their own deltas.

| plan | issue | dep | size | lands |
| --- | --- | --- | --- | --- |
| [01 — TIPC](./01_tipc_backend.md) | [#378] | **none** (stdlib) | small | first |
| [02 — QUIC/`iroh`](./02_quic_iroh_backend.md) | [#353] | `iroh` (uniffi FFI) | large | needs a prep PR |
| [03 — `wg` bindspace](./03_wg_tunnel_bindspace.md) | [#482], [#443] | `pyroute2` | medium, 3 layers | layer A now |

Headline conclusions:

- **TIPC is the cheap win.** Verified: `trio.SocketStream` and
  `trio.SocketListener` are address-family agnostic (only
  `SOCK_STREAM` + a trio socket), and CPython ships `AF_TIPC` +
  23 `TIPC_*` constants. So the backend is ~one module of
  contract boilerplate, zero new deps, and it buys kernel-native
  service primitives: `bind()` publishes, known-address
  `connect()` resolves, and topology events report publication
  changes. Actor-name lookup, registrar state and split-brain-safe
  election remain separate work. (`modprobe tipc` is required;
  hard-gate everything.)
- **QUIC's cost is entirely in two adapters**, not in QUIC. The
  `iroh` python bindings are `uniffi`-generated asyncio, but the
  asyncio dependency is confined to *one* future-poll callback —
  a ~40-line `trio` bridge (`TrioToken.run_sync_soon`) replaces
  it. The second cost is that an iroh listener isn't a socket,
  which needs a small, independently-reviewable prep PR to
  `_server.py`/`_types.py`.
- **WireGuard is not a transport.** It's an iface-layer tunnel,
  so it belongs as a *nested bindspace* (`TunnelledAddress` +
  `open_bindspace()` `@acm`s) wrapping whatever L4 tpt is in
  use — which is also what finally implements the long-spec'd
  `Address.namespace`, and what generalizes to
  `veth`/`vxlan`/`gre`.

Ordering rationale: plan 01 first as the cheap proof the
table-registration story generalizes to a genuinely new proto;
plan 03 layer A is already deployable-today doc/example work;
plan 02 last (and gated on its prep PR). Plans 01 and 02 both
want the same `Address.rebind_from_sockname` gate — whichever
lands first ships it.

[#378]: https://github.com/goodboy/tractor/issues/378
[#353]: https://github.com/goodboy/tractor/issues/353
[#482]: https://github.com/goodboy/tractor/issues/482
[#443]: https://github.com/goodboy/tractor/issues/443
