# `tractor` over a WireGuard tunnel, declared as one maddr

A two-host LAN setup: a `tractor` actor tree on host A, dialed
from host B, with the endpoint declared as a single `wg`
multiaddr.

Supersedes the example set in gh
[#482](https://github.com/goodboy/tractor/issues/482) — see
[what changed](#what-changed-vs-482).

> **Why `examples/multihost/`?** `tests/test_docs_examples.py`
> walks `examples/` recursively and runs everything it collects
> as a subproc, asserting `rc == 0`. These need a real second
> host and a live `wg` tunnel, so they can't satisfy that;
> `'multihost' not in p[0]` is already in the test's exclusion
> list, which is what keeps them out of CI.

## the maddr form

```
/ip4/192.168.1.50/udp/51820/wg/u<A_pub>/ip4/10.0.11.1/tcp/1616
\____ wg bearer ___________/\__ key __/\____ tractor ep _____/
 underlay, wg `ListenPort`              overlay, on the wg iface
 (kernel owns the socket)               (`MsgTransport` binds this)
```

Three parts, three different owners:

| part | socket owner / provisioner | runtime role |
| --- | --- | --- |
| `/ip4/../udp/51820` bearer | kernel-owned; `wg-quick` now, tractor bindspace later | control-plane metadata |
| `/wg/u<key>` | nothing — it's an identity | parsed, verified explicitly |
| `/ip4/../tcp/1616` overlay | `tractor`'s `IPCServer` | application `MsgTransport` |

Verified against py-multiaddr
[#108](https://github.com/multiformats/py-multiaddr/pull/108):
this composed form parses and round-trips
(`['ip4','udp','wg','ip4','tcp']`).

## requirements

py-multiaddr #108 is **merged** (2026-07-28) but ships in no
release yet — the latest `0.2.0` (2026-03-17) predates it and has
no `wg` codec. So `pyproject.toml` temporarily pins the merge commit
in its PEP 621 dependency metadata, and a plain

```bash
uv sync
```

gets you a `wg`-aware `multiaddr`. That pin goes away once a
release carries the codec. `py-multibase` is a direct dependency.

Without the codec `parse_wg_maddr()` raises immediately with an
actionable message — there is deliberately **no** degraded
hand-split fallback. `_wg_proto_code()` performs the capability
check before parsing.

Every peel and re-compose here goes through `py-multiaddr`'s own
tunnel API (`.decapsulate_code()`, `.split()`, `.join()`,
`.encapsulate()`, `.value_for_protocol()`) rather than any
bespoke segment slicing — see its README "En/decapsulate" and
"Tunneling" sections. gh #429 was about *dropping* our NIH
parser, and that applies to peeling a tunnel stack just as much
as to decoding one proto.

## 0. tunnel setup (out-of-band, both hosts)

Host A is the service host (underlay e.g. `192.168.1.50`), host B
your workstation. Overlay net `10.0.11.0/24`.

```bash
umask 077
wg genkey | tee wg_priv.key | wg pubkey > wg_pub.key
```

`/etc/wireguard/wg0.conf` on **host A**:

```ini
[Interface]
PrivateKey = <A_priv>
Address = 10.0.11.1/24
ListenPort = 51820
```
```ini
[Peer]
PublicKey = <B_pub>
AllowedIPs = 10.0.11.2/32
```

on **host B**:

```ini
[Interface]
PrivateKey = <B_priv>
Address = 10.0.11.2/24
```
```ini
[Peer]
PublicKey = <A_pub>
Endpoint = 192.168.1.50:51820
AllowedIPs = 10.0.11.1/32
PersistentKeepalive = 25
```

Note how `ListenPort` and `Endpoint` are exactly the maddr's
bearer segment, and `[Interface] Address` is its overlay host.

```bash
sudo wg-quick up wg0   # both hosts
ping -c1 10.0.11.1     # from B
```

## 1. get your pubkey into the maddr

```bash
python -c "
 from tractor.discovery import mb_pubkey
 key = open('wg_pub.key').read().strip()
 print(mb_pubkey(key))
"
```

Paste the `u...` output into `WG_MADDR` in both scripts (they use
the same string — A's bearer, A's key, A's overlay ep).

## 2. run

```bash
# host A
python host_a_srv.py

# host B
python host_b_client.py
```

`host_a_srv.py` must be importable on host B too, since
`portal.run()` refs the fn by module path — standard `tractor`
RPC semantics.

## what changed vs #482

Four corrections, all from
`ai/tpt-backends/03_wg_tunnel_bindspace.md`:

1. **the maddr semantics were inverted.** #482 used
   `/ip4/10.0.11.1/tcp/1616/wg/u<key>` — that parses, but it puts
   the *overlay* addr where the bearer belongs and `tcp` where
   wg's `udp` `ListenPort` goes, and it declares no overlay ep at
   all. `parse_wg_maddr()` now rejects it with an actionable
   error.
2. **parsing is pure.** #482's helper had the key-check adjacent
   to the parse; `verify_wg_peer()` is now a separate, explicitly
   composed step that the caller invokes. A parser that shells
   out is a nasty surprise.
3. **no `sudo`.** #482 ran `sudo wg show`; a library/example must
   never escalate. `wg show` works unprivileged for read on most
   setups; if yours needs root, run the script as root rather
   than embedding `sudo`.
4. **no new `Address` proto-type.** The tunnel rides *beside* the
   overlay addr in a frozen `TunnelledAddress`, and only `.overlay`
   crosses into `open_nursery()`. #482 §6 floated a `WGAddress`
   registered in `_address_types` — that table is a `bidict`
   (1:1 proto-key↔type) and `_addr_to_transport` wants a
   `MsgTransport` per addr-type, which `wg` doesn't have.

## next

Layer A's `TunnelledAddress` and native maddr parser now live in
`tractor.discovery`. Next, replace this example's `wg(8)` verification
probe with `pyroute2`, then add `open_bindspace()` `@acm`s which
create/tear down the iface and netns.
