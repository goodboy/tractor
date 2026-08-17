# TIPC backend — handoff

Status: **PR [#493] is feature-complete and green**; what remains
is landing logistics plus a named follow-up track.

Takeover snapshot (2026-08-17): [#493] remains a draft at
`c7ae6065`, targeting `ng_tpts_planning` ([#492]). Its branch
point is `ee17ed9f`; #492 has since advanced by two commits to
`d9a6e2e9`, so #493 still needs rebasing onto that current
[#492] head before final landing work. The latest `tipc` CI leg
passed; the workflow as a whole is red only because the macOS
`tcp` leg failed.

Audience: any agent or human picking this up cold, from any
provider. Nothing here assumes a particular harness or tooling.

Read in this order,

1. this file (orientation + what's already settled)
2. [`00_shared_backend_contract.md`](./00_shared_backend_contract.md)
   — **normative** description of what a `tractor` transport
   backend *is*
3. [`01_tipc_backend.md`](./01_tipc_backend.md) — the plan,
   already reconciled against the live-kernel findings

Do **not** re-derive the design or re-select libraries. Where
this doc and the code disagree, **the code wins** — fix the doc
in the same change (contract §0).

[#493]: https://github.com/goodboy/tractor/pull/493

---

## 1. Environment

The backend needs a linux kernel module that is **not loaded by
default**:

```bash
sudo modprobe tipc
tipc node get address        # confirms the module is live
```

Everything else is stdlib — TIPC adds **zero** dependencies.

This repo is a git worktree with a `uv`-managed venv at
`./py313`. Run things through it:

```bash
./py313/bin/python -m pytest tests/ipc -q
./py313/bin/python -m pytest tests/ -q --tpt-proto tipc
```

To rebuild docs you need the docs dep-group, which *mutates*
that venv — `uv sync` afterwards to restore it:

```bash
UV_PROJECT_ENVIRONMENT=py313 uv run --group docs \
    python -m sphinx -b html docs /tmp/docbuild
UV_PROJECT_ENVIRONMENT=py313 uv sync
```

Without the module, `--tpt-proto tipc` fails loudly and
immediately (by design); `tests/ipc/test_tipc.py`'s
kernel-touching cases self-skip.

## 2. What the backend is, in three sentences

An actor's TIPC address is a **service name** `(stype, instance)`
— no host, no port. Binding the singleton `TIPC_ADDR_NAMESEQ`
range *publishes* it into a kernel-maintained cluster-wide name
table (visible via `tipc nametable show`), and a peer's
`.connect()`-by-name *is* the discovery lookup, resolved
in-kernel. `TIPC_ADDR_ID` port-ids are only ever **observed**,
never user-facing.

Everything lives in `tractor/ipc/_tipc.py`.

## 3. Hard-won facts — do not re-litigate these

All verified against a live kernel. Several contradict what the
plan originally assumed.

| fact | why it matters |
| --- | --- |
| A duplicate name bind **succeeds**, and connects **round-robin** between publishers | An instance collision is *silent crosstalk*, never `EADDRINUSE`. Hence the `blake2b` instance digest. |
| Dialing an unpublished name gives `EHOSTUNREACH` **instantly**, as a **bare `OSError`** — not a `ConnectionError` subtype | `_reraise_as_connerr()` is REQUIRED by contract §4, not polish |
| `getsockname()` always answers a `TIPC_ADDR_ID` port-id, even pre-bind | why `TIPCAddress.rebind_from_sockname = False` |
| A connect-then-drop peer makes `getpeername()` raise `ENOTCONN` | Unguarded, this **kills the whole actor** — `.get_stream_addrs()` runs *before* the handshake, so it escapes handshake tolerance. See `_maybe_sockaddr()`. |
| `SO_ACCEPTCONN` works fine (answers `1`) | trio's `except OSError` carve-out is not load-bearing here |
| Graceful peer close arrives as `BrokenResourceError`/`ECONNRESET`, not a clean 0-byte EOF | benign; `_iter_packets()` already classifies it as a normal disconnect |
| Topology `struct tipc_event` is **48 bytes** (`4+4+4+8+28`) | the plan said 40 |
| The topology server **accepts native `'='` byte-order** | the plan's proposed `'>'`-retry endianness probe was deleted as unnecessary |
| `TIPC_WAIT_FOREVER` is `-1` in python | must be masked (`& 0xFFFFFFFF`) before packing as unsigned |
| TIPC **has** AES-GCM encryption (`tipc node set key`, linux 5.9+) | cluster/master/per-node keys + rekeying. So "wg adds the encryption TIPC lacks" is **false** — see §6 for the real motivation. |
| A wg interface is L3/`tun` (`POINTOPOINT,NOARP`, `link/none`) | TIPC's `eth` media **cannot** bind it — udp media is *mandatory* over wg. See the §6 caveat; this one bites. |

Two design decisions that are **closed**, with reasons:

- **The accepting side does not learn the peer's service name.**
  A port-id can't be reversed into one. It gets a
  `TIPC_NAME_UNKNOWN` sentinel plus the observed `(node, ref)`,
  and that's fine — the `Aid` from the handshake already carries
  the peer's logical identity. (`uds` has the same wart.)
- **Do not fold digest bits into the service `_stype` to widen
  the collision space.** A topology subscription can only watch
  **one** `stype`, so varying it per-actor would need 65536
  subscriptions and kills the push-registry work outright. If
  crosstalk ever bites, the escalation is a post-bind
  verification handshake ([#501]).

[#501]: https://github.com/goodboy/tractor/issues/501

## 4. What landed

All §6 steps 1–7 landed on `wkt/tipc_backend_378`, based on
`ng_tpts_planning` (PR [#492], docs-only). The completed arc is
17 substantive commits from `22ef362d` through `c7ae6065`, plus
the incidental `1802641e` local-cache ignore commit. The plan's
§6 status text is the historical snapshot at `7e20585f`; its
claim that steps 6–7 remain is no longer current.

- `TIPCAddress` + `is_tipc_available()` + `start_listener()`
- `MsgpackTIPCStream` (`connect_to()`, `get_stream_addrs()`)
- `open_topology_events()` — the `TIPC_TOP_SRV` push feed
- registration across every table in contract §2
- interim `str`-only `/tipc/…` maddr grammar
- a `--tpt-proto=tipc` CI leg, **non-blocking** for now
- `docs/guide/tipc.rst` + `examples/multihost/tipc_cluster/`

Two fixes fell out that are **not** TIPC-specific:

- `devx/pformat.py` — `pformat_caller_frame()` passed an
  `indent=''` kwarg `pformat_boxed_tb()` never accepted, so every
  send-side `MsgTypeError` died with a `TypeError` while
  formatting itself. On `main` and every branch since
  `888af602`. **Wants cherry-picking out of this stack.**
- `SpawnSpec.reg_addrs`/`.bind_addrs` pinned the wire shape to a
  2-tuple. Widened to `UnwrappedAddress`, which had to become
  **variadic** (`tuple[str|int, ...]`) because `msgspec` refuses
  a union holding more than one array-like type.

**Acceptance bar met**: 122 passed / 1 xfailed / 2 xpassed under
`--tpt-proto tipc` across `ipc`, `discovery`, `runtime`,
`spawning`, `local`, `rpc`, `cancellation`. `tcp`/`uds`
unchanged.

[#492]: https://github.com/goodboy/tractor/pull/492

## 5. Immediate next steps (pre-land)

These live on [#493]'s body as `### TODOs before landing`. They
are **not** mirrored into an issue — if the PR is ever superseded
they need re-homing.

1. **Cherry-pick the `pformat` fix onto `main`** as its own PR,
   and land it before [#493] —
   `22ef362d` (red guard test) then `f9f98eeb` (the 1-line fix).
   Preserve that order. The pair is unrelated to TIPC, every
   branch has the bug, and this fix must not disappear if #493
   is superseded.
2. **Watch the `tipc` CI leg.** It's gated
   `continue-on-error: ${{ matrix.tpt_proto == 'tipc' }}` because
   GH's runners have never been asked to `modprobe` for us. Once
   it has a few green runs, drop the gate. If the runners refuse
   the `modprobe`, fall back to a container job with
   `--cap-add NET_ADMIN`.
3. **Rebase #493 onto #492's current head first.** At this
   snapshot that means moving from the `ee17ed9f` branch point
   onto `d9a6e2e9`. #492 is checked out in another worktree, so
   refresh its authoritative head and coordinate before changing
   history. Once #492 merges, rebase #493 onto `main` for final
   landing.

## 6. The follow-up track

All filed with the `follow-up` label.

| issue | what |
| --- | --- |
| [#495] | `TIPC_IMPORTANCE` supervision QoS on the parent↔child chan |
| [#496] | `TIPC_TOP_SRV` push registry in `discovery._registry` |
| [#497] | dual-link resiliency / multi-homing |
| [#498] | `/tipc` multiaddr spec submission |
| [#499] | registrar-less discovery via name derivation |
| [#500] | multicast/group msging as a *broadcast* transport |
| [#501] | post-bind verification for instance collisions |
| [#502] | **TIPC over a `wg` mesh — the reference multihost deployment** |

### the wg direction

[#502] is the strategic one. The intent is that TIPC-over-`wg`
becomes our go-to multihost transport deployment.

> ⚠️ **Do not repeat the claim that wg adds encryption TIPC
> lacks.** We assumed that initially and it is **wrong**. TIPC
> ships AES-GCM crypto of its own (`tipc node set key`, linux
> 5.9+) with cluster/master/per-node keys and rekeying.
>
> The motivation is different but still real:
> - **key management** — TIPC keys are symmetric and
>   *pre-shared*; distribution, rotation and revocation are the
>   operator's problem. wg gives public-key identity + handshake.
> - **uniformity** — wg is an overlay *every* backend can sit on
>   (tcp now, quic later), not a TIPC-only mechanism.
> - **NAT traversal / roaming**, which raw TIPC bearers have no
>   story for.
>
> Which to default to should be **benchmarked**, not assumed:
> TIPC-native crypto avoids a tunnel hop and may win on latency
> for LAN-local clusters.

> ⚠️ **`udp` media is MANDATORY over wg — `eth` cannot work.**
> A wg interface is L3/`tun`: `POINTOPOINT,NOARP`, `link/none`,
> no L2 address at all. There is no device for `tipc bearer
> enable media eth device …` to name.
>
> ```bash
> # impossible over wg
> sudo tipc bearer enable media eth device wg0
> # required, bound to the wg overlay IP
> sudo tipc bearer enable media udp name wgmesh localip 10.0.11.1
> ```
>
> Consequence worth internalizing: #378's "ethernet bearers pair
> most excellently with wireguard tunnelling" framing does **not**
> hold — on a given link the low-latency L2 path and the wg path
> are *mutually exclusive*. Any design that assumes both is
> broken from the start.
>
> Also mind the MTU: wg links are typically 1420, under
> ethernet's 1500, so TIPC link MTU wants checking not assuming.

Composed addresses take the form:

```
/ip4/<pub>/udp/51820/wg/u<key>/tipc/<stype>/<inst>/<scope>
\____ wg bearer ________/\_key_/\______ tractor ep ________/
```

Note the structural point, which matters for the spec proposal
in [#498]: the tcp equivalent repeats an `/ip4/…/tcp/…` inner
segment because a tcp endpoint is *located*. The tipc inner
segment has **no locative component at all** — a service name is
location-independent by design. So in the composed form the wg
segments carry all the routing and the tipc segment carries pure
*identity*.

Prerequisites already established:

- py-multiaddr [#108] (merged) proved the composed `wg` + tcp
  form parses and round-trips (`['ip4','udp','wg','ip4','tcp']`)
- `examples/multihost/wg_lan/` is the existing wg example set to
  generalize from rather than duplicate
- the udp-bearer-only constraint and MTU caveat are documented in
  both `docs/guide/tipc.rst` and the `tipc_cluster` README

[#495]: https://github.com/goodboy/tractor/issues/495
[#496]: https://github.com/goodboy/tractor/issues/496
[#497]: https://github.com/goodboy/tractor/issues/497
[#498]: https://github.com/goodboy/tractor/issues/498
[#499]: https://github.com/goodboy/tractor/issues/499
[#500]: https://github.com/goodboy/tractor/issues/500
[#502]: https://github.com/goodboy/tractor/issues/502
[#108]: https://github.com/multiformats/py-multiaddr/pull/108

## 7. Working conventions in this repo

Provider-neutral, but they *are* enforced by review:

- **Never commit, push, rebase or amend on your own.** Prepare
  changes, report them, and let the maintainer stage. Asking
  "should we commit?" is a question *for you to answer*, not
  permission to act.
- **Do not re-ask for an exact forge write already authorized in
  the current request.** Use the provider adapter's snapshot,
  digest and drift checks, perform the named edit, then report
  what was published. This does not authorize unrelated or
  destructive forge actions.
- **A failing/guard test lands in its own commit before the fix
  it guards.** Red first, then green.
- **One commit per logical step**, so history shows *why*. Never
  squash unrelated changes.
- Commit subjects: present-tense verb, ~50 chars (hard max 67),
  backticks around every code element. Bodies wrap at 67 cols.
- **Never write a line containing only whitespace.**
- Annotate everything including locals; prefer `match`/`case`
  over `isinstance` chains; multi-line calls with trailing
  commas; `@acm` over classes with `.start()`/`.stop()`.
- New modules get the `# tractor: distributed structured
  concurrency.` header tagline plus the AGPL block.
- **Do not change task/checkbox state** in issues, plans or
  trackers unless explicitly asked for that exact transition.
- Fix warnings at source; only genuinely-unfixable ones get
  filtered, with a documented reason.

## 8. Where things are

```
tractor/ipc/_tipc.py                     the whole backend
tests/ipc/test_tipc.py                   28 backend unit tests
tests/ipc/test_server.py                 the reconciliation guard
tests/devx/test_pformat.py               the cherry-pick candidate
docs/guide/tipc.rst                      the docs page
examples/multihost/tipc_cluster/         runnable demos + manual
                                         smoke test
ai/tpt-backends/00_shared_backend_contract.md
ai/tpt-backends/01_tipc_backend.md       the (reconciled) plan
.github/workflows/ci.yml                 the gated tipc leg
```

Both single-host examples have been **run against a live
kernel** — the output pasted in their README is real, not
illustrative.

External agent memory deliberately contains only a project
pointer back to this handoff, not a competing copy of the project
state. Treat this file as the durable source of truth and update
it when the branch topology or landing sequence changes.
