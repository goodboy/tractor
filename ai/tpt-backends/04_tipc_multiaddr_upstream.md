# `/tipc` multiaddr protocol: upstream issue draft

Candidate issue for `multiformats/multiaddr`, to be submitted after
the encoding questions below have been reviewed locally.

## Context

Linux TIPC (Transparent Inter-Process Communication) addresses a
service by a location-independent `(service type, instance)` name.
A server publishes that name into the kernel-maintained cluster name
table and a client connects by the same name; no host or transport
port forms part of the service identity.

TIPC is also known as **Cluster Domain Sockets**, a useful description
of its relationship to Unix-domain sockets. The registered protocol
name should nevertheless remain `tipc`: it matches Linux's
`AF_TIPC`, socket constants, kernel module and iproute2 frontend.
Registering `cds` would create an ecosystem-specific alias that is
harder to map back to the normative kernel API.

We would like to register a `tipc` multiaddr component so these
service endpoints can be represented directly and composed with a
bearer or tunnel description:

```text
/tipc/1953628160:1616:2

/ip4/192.168.1.50/udp/51820/wg/u<key>/tipc/1953628160:1616:2
\____________ WireGuard bearer ____________/\____ TIPC service ____/
```

In the composed form, the components through `/wg/<key>` identify
the routed bearer and tunnel peer. The `/tipc/...` component is pure
service identity, resolved to a current publisher by the TIPC kernel
name table. Unlike a TCP endpoint, it deliberately has no inner IP
address or port.

This proposal does not imply that parsing the multiaddr configures a
TIPC bearer. In particular, TIPC over a WireGuard interface requires
a separately configured TIPC UDP bearer; WireGuard interfaces are L3
devices and cannot carry TIPC Ethernet media directly.

Today that bearer is configured through iproute2's `tipc` frontend,
which speaks the kernel's `TIPCv2` generic-netlink family. `pyroute2`
already provides WireGuard support and generic-netlink primitives but
has no TIPC codec/module; adding one is a complementary deployment
automation track, not part of this address-format proposal.

## Proposed protocol

- Name: `tipc`
- Code: TBD, allocated in `multiformats/multicodec` under the
  `multiaddr` tag before implementations stabilize one
- Size: 72 bits
- Value: service type, service instance and publication scope

### Binary form

Exactly nine bytes with no value-length prefix:

| Offset | Size | Field | Encoding |
| ---: | ---: | --- | --- |
| 0 | 4 bytes | service type | unsigned 32-bit big-endian |
| 4 | 4 bytes | service instance | unsigned 32-bit big-endian |
| 8 | 1 byte | publication scope | unsigned enum byte |

```text
tipc-value = uint32be(type) || uint32be(instance) || uint8(scope)
```

For type `1953628160` (`0x74720000`), instance `1616` and
cluster scope `2`, the payload is:

```text
74 72 00 00 00 00 06 50 02
```

### String form

Use one multiaddr value segment containing three canonical decimal
integers:

```text
/tipc/<type>:<instance>:<scope>
```

Canonical values have no sign, whitespace, alternate radix or
leading zeroes, except that zero itself is `0`. `type` and `instance`
must fit unsigned 32-bit fields. Scope is one of:

- `2`: `TIPC_CLUSTER_SCOPE`
- `3`: `TIPC_NODE_SCOPE`

The existing experimental spelling
`/tipc/<type>/<instance>/<scope>` cannot be registered as one normal
multiaddr protocol: generic parsing treats each slash-delimited name
as another protocol component. A single structured value preserves
TIPC's atomic service-address semantics without registering three
artificial protocols.

## Why scope is included

TIPC scope controls where a bound service publication is visible.
The same address representation is used for listener configuration
and dialing, so retaining scope lets a multiaddr round-trip the full
socket address rather than silently turning a node-local bind into a
cluster publication.

Modern Linux UAPI defines cluster and node scopes. The deprecated
zone spelling should not receive a new wire value; implementations
may normalize legacy input to cluster scope before encoding.

## Composition

Standalone service:

```text
/tipc/1953628160:1616:2
```

TIPC service reached through a WireGuard bearer:

```text
/ip4/192.168.1.50/udp/51820/wg/u<key>/tipc/1953628160:1616:2
```

This differs intentionally from TCP over WireGuard:

```text
/ip4/192.168.1.50/udp/51820/wg/u<key>/ip4/10.0.11.1/tcp/1616
```

TCP repeats an inner locative address. TIPC does not: its service
name is resolved and load-balanced in-kernel across current
publishers.

## Semantics and security

- A TIPC service name identifies a service, not a unique process.
  Multiple publishers may bind the same name and connections can be
  distributed among them.
- Publication scope is reachability metadata, not authentication.
- A composed `/wg` key authenticates the tunnel peer, not the TIPC
  service publisher.
- TIPC's optional native AES-GCM link encryption is independent of
  this address codec and of WireGuard.
- Codec implementations should validate field widths and canonical
  text only; cluster membership and publisher authorization remain
  deployment concerns.

## Implementation plan

1. Reserve a `multiaddr`-tagged code in
   `multiformats/multicodec`.
2. Add the fixed-size protocol row and normative encoding text to
   `multiformats/multiaddr`.
3. Add codecs and cross-language test vectors, beginning with
   `multiformats/py-multiaddr`.
4. Verify standalone and composed `wg` + `tipc` string/binary
   round-trips.

## Open questions

1. Is a fixed 72-bit value preferred over a self-describing or
   variable-width tuple for this kernel-defined address?
2. Should node scope be representable in a generally shareable
   multiaddr, or should the registered form be cluster-only?
3. Does multiaddr have an existing convention for structured numeric
   values that should replace the colon-separated text form?
4. Should the specification describe TIPC service *ranges*, or keep
   this protocol limited to singleton service names used for
   connection endpoints?

## References

- Linux TIPC documentation:
  https://docs.kernel.org/networking/tipc.html
- Cluster Domain Sockets terminology:
  https://en.wikipedia.org/wiki/Transparent_Inter-process_Communication
- Linux socket UAPI:
  https://github.com/torvalds/linux/blob/master/include/uapi/linux/tipc.h
- Linux TIPC generic-netlink UAPI:
  https://github.com/torvalds/linux/blob/master/include/uapi/linux/tipc_netlink.h
- pyroute2 WireGuard and generic-netlink APIs:
  https://docs.pyroute2.org/wireguard.html
- WireGuard multiaddr implementation discussion:
  https://github.com/multiformats/py-multiaddr/issues/107
- WireGuard codec implementation:
  https://github.com/multiformats/py-multiaddr/pull/108
- Downstream tracking and prototype:
  https://github.com/goodboy/tractor/issues/498
