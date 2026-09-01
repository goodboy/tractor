# Declarative WireGuard actor bootstrap

This plan starts from `d80dcbe6`, after the explicit WireGuard,
bindspace, root-netns and Trio-child bootstrap work in PR #511.
It proposes the final declaration-driven layer only. Production and
test implementation remain outside this plan commit.

## Goal

Turn an actor-name endpoint table into explicitly owned network
resources that configure one root and its named Trio children without
moving declarations, WG secrets or lifecycle policy into actor runtime
parameters.

The completed API should let an application:

1. parse a flat endpoint table;
2. combine each actor's declarations with local realization policy;
3. realize every required network resource before root startup;
4. pass concrete addresses and live bindspaces through existing root
   and child APIs; and
5. retain every owned resource until the complete actor tree is reaped.

For attached resources, the coordinator pins the namespace FD only.
The external owner must keep each borrowed interface present and
unchanged until the endpoint-tree context exits.

## Existing boundaries

The implementation composes, rather than replaces, these contracts:

- `parse_endpoints()` parses a flat
  `dict[str, list[address declaration]]` and preserves ordered
  `TunnelledAddress` wrappers. It neither selects an actor nor opens
  resources (`tractor/discovery/_multiaddr.py:170-227`).
- A parsed `WGTunnelSpec` carries public path identity. Local interface
  selection and `WGInterfaceConfig` remain process-local policy
  (`tractor/net/_tunnel.py:116-149`,
  `tractor/net/_tunnel.py:218-368`).
- `open_wg_bindspace()` manages one bindspace and an ordered stack of
  owned WG interfaces (`tractor/net/_tunnel.py:637-687`).
- `open_root_actor()` accepts concrete addresses and one already-live
  `Bindspace`; it enters that namespace before registry or IPC work
  (`tractor/_root.py:183-311`).
- `ActorNursery.start_actor()` accepts concrete child bind tuples and
  one live `Bindspace`; Trio spawn transports a duplicate FD before
  child runtime startup (`tractor/runtime/_supervise.py:416-513`).
- `SpawnSpec.bind_addrs` remains concrete tuples because msgspec cannot
  decode the abstract address union
  (`tractor/msg/types.py:193-220`).

## Decisions proposed for review

### Keep declaration and runtime layers separate

Do not add an endpoint table, WG secrets or lifecycle policy to
`open_root_actor()` or `ActorNursery.start_actor()`.

Add an outer `tractor.net` planner and resource context. The caller
continues to pass only:

- realized declarations and a live bindspace to the root;
- concrete unwrapped bind addresses and a live bindspace to Trio
  children; and
- registrar addresses explicitly, because remote registry discovery
  is separate from local actor binding.

### Start with a flat, exact-name table

The first implementation supports exact actor-name keys only.

- `worker` never matches `worker-2` or a prefixed cluster name.
- A missing name means no declarative override; callers may retain
  existing defaults.
- An explicitly empty endpoint list is rejected by planning rather
  than silently selecting a random listener.
- Planning snapshots input lists and never pops or mutates caller
  configuration.
- One table entry describes one actor instance. Reusing its fixed
  binding for concurrent actors violates the API precondition; no
  runtime claim registry is added in this phase.

Recursive tables and instance allocation remain separate API design
work. Their matching and ownership semantics are not defined by the
original parser.

### Separate declaration identity from local realization

An maddr does not choose the local interface or namespace. Pair each
declared tunnel with explicit process-local realization policy:

```python
WGInterfaceLifecycle = Literal['attach', 'open']


class EndpointLayerConfig(ProcessLocal):
    spec: WGTunnelSpec
    lifecycle: WGInterfaceLifecycle
    config: WGInterfaceConfig | None = None


class EndpointConfig(ProcessLocal):
    bindspace_spec: BindspaceSpec
    layers: tuple[EndpointLayerConfig, ...] = ()
    role: WGRole = 'listen'
```

`EndpointLayerConfig.spec` is the local realization. Planning matches
it to the parsed declaration by tunnel depth and maddr-defined fields:

- `peer_pubkey` and `bearer` must match the declaration;
- local `iface` and `netns` may replace parser defaults;
- an explicit local `netns` must agree with
  `BindspaceSpec.key`; and
- layer order remains outermost first.

Planning retains `peer_pubkey`, `bearer` and wrapper order as the public
declaration identity, but does not preserve parsed local `iface` or
`netns` values in the realized graph. After setup, recursively rebuild
every tunnel wrapper from the concrete overlay outward with its matched
local spec and the same realized `BindspaceRef`. This canonical graph
cannot carry a stale parsed namespace or leave an inner wrapper
unannotated. It also avoids calling `with_bindspace_ref()` on an
incompatible retained spec.

For `lifecycle='open'`, `config` is required and the interface is
created and removed. For `lifecycle='attach'`, `config` is absent; the
coordinator verifies an existing interface but never creates, mutates
or deletes it. Attachment is a checked borrowing contract, not an
interface lease: the caller must arrange an external owner that keeps
the interface alive and configured until context exit.

Listen declarations identify the local WG public key. After managed
creation, or during attachment, read the actual interface key and
compare it with the declaration before publishing a binding. Dial
declarations identify a remote peer and use explicit peer verification.
No key relationship is inferred from private material alone.

### Require one network stack per actor process

A process enters one active network namespace for runtime startup.
Planning therefore enforces:

- all tunnel-bearing endpoints for one actor declare one common
  ordered tunnel stack;
- local layers match that stack by declared identity and depth;
- plain addresses may coexist inside the same bindspace;
- a bindspace-only profile may use an empty layer tuple; and
- unknown config keys, missing layers, extra layers, incompatible
  stacks or namespace mismatches fail before async setup.

Planning also checks static collisions across actors that resolve into
the same bindspace:

- duplicate fixed TCP/UDS bind addresses;
- duplicate managed interface names; and
- duplicate ownership claims for one named bindspace.

This catches configuration collisions but does not police later reuse
of one binding by arbitrary runtime calls.

### Snapshot before the first checkpoint

Existing process-local WG config structs are mutable. A frozen outer
tuple does not freeze nested key, peer or route values.

Add a frozen process-local marker beside `ProcessLocal` for endpoint
records, and prove default msgspec encoding still fails. More
importantly, `open_endpoint_tree()` must synchronously copy every
mutable nested field into private coordinator-owned values before its
first `await`. Setup and cleanup never reread caller-owned plans.

The proposed public outputs are:

```python
class EndpointPlan(FrozenProcessLocal):
    name: str
    declared_addrs: tuple[Address | TunnelledAddress, ...]
    bind_addrs: tuple[Address, ...]
    config: EndpointConfig | None


class EndpointBinding(FrozenProcessLocal):
    name: str
    declared_addrs: tuple[Address | TunnelledAddress, ...]
    bind_addrs: tuple[Address, ...]
    bindspace: Bindspace | None
```

`EndpointBinding` does not expose WG secrets or mutable setup config.
Its tunnel declarations are recursively rebuilt with canonical local
specs and the realized `BindspaceRef` so every layer agrees with the
live bindspace and root endpoint diagnostics report its namespace
inode. The existing single-wrapper `with_bindspace_ref()` helper is
insufficient when parsed local fields differ or wrappers are nested
(`tractor/net/_tunnel.py:886-909`,
`tractor/net/_tunnel.py:946-976`).

### Realize the complete table before root startup

Add lazy public APIs from `tractor/net/_bootstrap.py`:

```python
def plan_endpoints(
    endpoints: ParsedEndpoints,
    configs: Mapping[str, EndpointConfig],
) -> dict[str, EndpointPlan]:
    ...


@acm
async def open_endpoint_tree(
    plans: Mapping[str, EndpointPlan],
) -> AsyncIterator[Mapping[str, EndpointBinding]]:
    ...
```

`open_endpoint_tree()` enters plans in table order through one
`AsyncExitStack` and publishes a read-only binding mapping only after
all resources and identity checks are ready. It opens no resource
lazily from actor-spawn tasks.

The intended call shape is:

```python
parsed = tractor.net.parse_endpoints(endpoint_table)
plans = tractor.net.plan_endpoints(parsed, local_configs)

async with tractor.net.open_endpoint_tree(plans) as endpoints:
    root_ep = endpoints['pikerd']
    async with tractor.open_root_actor(
        name='pikerd',
        tpt_bind_addrs=list(root_ep.declared_addrs),
        bindspace=root_ep.bindspace,
        registry_addrs=registry_addrs,
    ):
        async with tractor.open_nursery() as an:
            child_ep = endpoints['brokerd']
            portal = await an.start_actor(
                'brokerd',
                bind_addrs=[
                    addr.unwrap()
                    for addr in child_ep.bind_addrs
                ],
                bindspace=child_ep.bindspace,
            )
            ...
```

The nesting is contractual: the endpoint tree encloses the root and
actor nursery so networking cannot tear down around a live actor.

## Concurrency and cleanup contract

`open_endpoint_tree()` has no shared mutable cache, runtime claim map
or lazy setup path.

The setup path is:

1. validate and privately snapshot every plan synchronously;
2. enter actor A's bindspace and layers;
3. enter actor B's bindspace and layers;
4. verify local listen keys or dial peers;
5. recursively construct canonical declarations whose every wrapper
   carries the live bindspace ref;
6. publish a read-only binding mapping; and
7. let caller tasks start actors from already-live capabilities.

Every async-context entry is a checkpoint. A failure or cancellation
must close the entered prefix before it escapes. No task can observe a
partial binding mapping, and caller mutation after the private snapshot
cannot affect setup.

After yield, root and Trio startup duplicate borrowed namespace FDs
rather than consuming coordinator handles. Concurrent child starts
read capabilities but do not mutate coordinator state.

The teardown path is:

1. child actors stop and are reaped;
2. root listeners, runtime and thread restoration complete;
3. actor WG layers close inside-out; and
4. actor bindspaces release last.

Plain `AsyncExitStack` does not preserve this plan's error policy by
itself. Lower WG and netns managers can currently mask a body or setup
error when cleanup fails. Before exporting the coordinator:

- shield every owned cleanup from cancellation;
- continue attempting all remaining cleanup;
- preserve the active setup/body/cancellation exception;
- attach later cleanup failures as notes; and
- make externally removed owned netns teardown idempotent.

Apply that policy inside `open_netns()`, `open_wg_iface()`, interface
creation rollback and the endpoint-tree stack. An outer manager cannot
repair a primary exception already replaced by an inner context.

## Route and privilege policy

### Explicit overlay routes

`_sync_create_wg_iface()` adds addresses and peer settings but no
explicit routes (`tractor/net/_tunnel.py:470-559`). Connected routes
cover only peers in the configured interface subnet.

Extend `WGInterfaceConfig` with an explicit `routes` tuple. Validate
each CIDR and add it to the newly owned interface without replacing an
existing host route. Interface deletion remains the owned route cleanup
boundary.

Do not infer host routes from peer `allowed_ips`: those values control
WireGuard peer selection and may exceed routes this process should own.

Tests must pre-create a conflicting route and prove provisioning fails
without changing it, while still rolling back only the new interface.

### Actionable privilege checks

Managed netns and WG creation are Linux-only and require authority in
the owning user namespace. Add diagnostics before the first resource
checkpoint:

- reject managed provisioning on non-Linux platforms;
- report a missing `tractor[wg]` dependency before partial setup;
- name `CAP_NET_ADMIN` for interface, address and route changes;
- name `CAP_SYS_ADMIN` where namespace creation or entry needs it; and
- wrap authoritative kernel `EPERM` with the failed operation,
  namespace and required capability.

An effective-capability mask is diagnostic only. User namespaces mean
the kernel operation remains authoritative.

Pre-provisioned attachment pins the existing namespace capability and
performs a point-in-time interface identity/configuration check. It
cannot pin a link against external deletion or reconfiguration, and it
owns no interface cleanup. Keeping that link stable through context
exit is an explicit external-owner precondition. Managed creation owns
all mutation and removal.

Strictly dropping provisioning capability before actor code conflicts
with same-process teardown, which needs that capability later. A helper
or persistent `wgman` is required to satisfy both drop-before-user-code
and managed cleanup. That architecture remains deferred; this phase
documents the limitation rather than claiming capability isolation.

## Test strategy

### Pure endpoint planning

Add `tests/net/test_bootstrap.py` with unit coverage for:

- exact matching beside similar names;
- absent versus explicitly empty declarations;
- defensive snapshots and preserved declaration order;
- mixed plain and tunnel-bearing endpoints;
- declaration-versus-realization field matching;
- canonical recursive rebuilding of parsed local fields;
- nested layer order;
- attach versus open interface policy;
- missing, extra and reordered layer configuration;
- differing tunnel stacks for one actor;
- bindspace/interface/address collisions across actors;
- plain bindspace-only profiles; and
- unknown local configuration keys.

No scheduler, monkeypatch or kernel resource is needed at this layer.

### Lower lifecycle regressions

Extend bindspace and WG lifecycle tests to prove:

- setup rollback preserves the setup error when removal also fails;
- body failure remains primary when WG/netns cleanup fails;
- cancellation cannot interrupt owned cleanup;
- all remaining cleanup still runs after one failure;
- externally removed named netns cleanup is tolerated;
- attached interfaces are verified but never mutated or deleted;
- attached-interface lifetime is not represented as coordinator
  ownership;
- managed listen identity matches the configured interface key; and
- dial attachment verifies the declared peer.

Use real Trio scheduling. Replace only pyroute2/kernel adapters where
the proof concerns exception ordering rather than kernel behavior.

### Coordinator lifecycle component tests

Use real Trio and `AsyncExitStack`, replacing bindspace and WG kernel
adapters with traceable async contexts.

Prove:

- every resource is ready before publication;
- plans enter in table order and exit in reverse order;
- each setup failure closes the entered prefix;
- body failure and cancellation close every resource;
- cleanup failures become secondary notes and do not stop cleanup;
- caller mutation during an entry checkpoint cannot alter setup; and
- every wrapper in published declarations contains the realized
  bindspace inode and canonical local spec.

The doubles retain context scheduling and ownership. They do not prove
pyroute2, kernel routing or `setns()`.

### Real root and Trio composition

Add Linux integration coverage that:

- plans distinct exact-name root and child entries;
- opens all resources before root startup;
- gives the root realized declarations and its live bindspace;
- gives the child concrete tuples and its alternate bindspace;
- observes the expected namespace in both actor bodies; and
- proves actors stop before owned endpoint resources unwind.

Fake only WG provisioning. Real namespace FDs, Trio process spawn,
handshake, supervision and cleanup remain material boundaries.

### Real WireGuard dataplane

Add a required, separately capable Linux system test that:

1. enters disposable user, mount and network namespaces;
2. establishes UID/GID mappings and private mount propagation;
3. provides an isolated writable `/var/run/netns` strategy for the
   existing named-netns implementation;
4. creates two named netns and a veth underlay through pyroute2;
5. opens one real WG endpoint profile in each namespace;
6. configures overlay addresses, peer policy and explicit routes;
7. starts the root in one namespace and a Trio child in the other;
8. completes the parent handshake over the WG overlay;
9. discovers the actor with `find_actor()` and performs one RPC; and
10. proves actor, socket, interface, route, namespace and FD teardown.

The veth pair is test-environment underlay, not a production bindspace
feature. The test replaces no networking or process boundary.

Skip only after a precise preflight proves an unavailable kernel
feature. Do not catch-and-skip after partial mutation. No `sudo`,
subprocess `wg` or sleep-based synchronization is allowed.

This commit is not considered a dataplane proof until the test passes
in an approved environment with WireGuard and the required namespace
capabilities. If no such CI runner or recorded local environment is
available, stop and report the system-test evidence as blocked rather
than merging a universally skipped scaffold.

## Implementation sequence

### Commit 1: Resolve endpoint realization plans

Files:

- add a frozen process-local marker in `tractor/msg/_local.py`;
- add `tractor/net/_bootstrap.py`;
- update lazy exports in `tractor/net/__init__.py`;
- add pure tests in `tests/net/test_bootstrap.py`; and
- add Prompt-IO provenance for substantive generated code.

Behavior:

- add layer/config/plan models and `plan_endpoints()`;
- separate parsed identity from local realization fields;
- define canonical recursive declaration reconstruction;
- enforce exact-name, one-stack and static-collision rules;
- snapshot declarations and expose concrete bind addresses; and
- reject ambiguous configuration before async setup.

Checks:

- Ruff on changed Python;
- process-local encoding and immutability tests;
- bootstrap, multiaddr and lazy-import tests; and
- full test collection.

### Commit 2: Harden owned and borrowed network lifecycles

Files:

- update netns cleanup in `tractor/net/_bindspace.py`;
- update WG rollback/cleanup and attachment in
  `tractor/net/_tunnel.py`;
- extend bindspace and WG lifecycle tests; and
- add Prompt-IO provenance.

Behavior:

- preserve primary errors and finish shielded cleanup;
- tolerate already-removed owned netns teardown;
- add explicit borrowed-interface verification with no mutation;
- document and test its external-owner lifetime precondition; and
- verify listen keys and dial peers before use.

Checks:

- Ruff;
- setup/body/cancellation/cleanup failure schedules;
- attached versus managed ownership tests;
- existing bindspace and WG lifecycle suites; and
- full test collection.

### Commit 3: Add routes and authority diagnostics

Files:

- extend WG config/provisioning in `tractor/net/_tunnel.py`;
- add a small Linux authority helper under `tractor/net/`;
- extend config, provisioning and privilege tests; and
- add Prompt-IO provenance.

Behavior:

- validate and own explicit route CIDRs;
- reject conflicting routes without replacing host state;
- distinguish attached from managed privilege requirements;
- fail before partial setup when support is clearly absent; and
- retain kernel errors while adding actionable capability context.

Checks:

- Ruff;
- fake-backed pyroute2 route and rollback tests;
- deterministic platform/dependency/capability cases;
- real unprivileged namespace probe where available; and
- full test collection.

### Commit 4: Open complete endpoint trees

This is the first commit exporting the resource-owning coordinator;
the lower safety, route and privilege contracts land first.

Files:

- extend `tractor/net/_bootstrap.py` and lazy exports;
- extend `tests/net/test_bootstrap.py`; and
- add Prompt-IO provenance.

Behavior:

- add `EndpointBinding` and `open_endpoint_tree()`;
- privately snapshot plans before any checkpoint;
- realize managed and attached layers eagerly;
- verify identities and recursively rebuild canonical declarations
  with bindspace refs on every wrapper;
- publish only after complete setup; and
- preserve primary errors through reverse-order cleanup.

Checks:

- Ruff;
- lifecycle and caller-mutation schedules;
- lower bindspace/WG suites; and
- full test collection.

### Commit 5: Compose a named actor tree

Files:

- add focused root/Trio endpoint-tree integration coverage;
- update `docs/api/net.rst`;
- update the two-host WG example; and
- add Prompt-IO provenance as required by code/test changes.

Behavior:

- demonstrate root and child argument projection;
- preserve root realized declarations for diagnostics;
- send concrete child tuples through `SpawnSpec`;
- keep endpoint resources outside root and nursery lifetimes; and
- leave low-level root, nursery and `SpawnSpec` signatures unchanged.

Checks:

- Ruff;
- real root/Trio netns composition with fake WG provisioning;
- docs build; and
- the affected root, spawn, IPC and network matrix.

### Commit 6: Prove the real WG dataplane

Files:

- add a Linux system test and isolated namespace fixture;
- add or update a capable CI job after explicit approval;
- document prerequisites and precise skip reasons; and
- add Prompt-IO provenance.

Behavior:

- provision the disposable veth/WG topology;
- prove handshake, discovery and RPC through WG;
- exercise routes and live bindspace FD propagation; and
- prove complete teardown without privilege escalation.

Checks:

- Ruff;
- a successful run in an approved capable Linux environment;
- ordinary CI collection and precise unavailable-feature skips;
- docs and package builds; and
- the full supported CI matrix.

## Deferred work

The following items remain outside this patch set:

- recursive endpoint tables and hierarchical actor-name matching;
- multiple concurrent actors claiming one fixed name/profile;
- multiprocessing alternate-bindspace FD transport;
- serializing tunnel declarations through `SpawnSpec` for child-side
  diagnostic retention;
- precise child bootstrap exceptions before IPC handshake;
- strict privilege drop with a dedicated `wgman` or helper process;
- shared-resource caching and dynamic lazy profile realization;
- production veth, VRF, VXLAN and other bindspace kinds;
- native tagged `TunnelledAddress` graph decoding; and
- replacing the temporary py-multiaddr VCS pin after a `/wg/` release.

## Review gates before implementation

Human review should confirm:

1. public names for layer config, endpoint config, plan and binding
   models plus `plan_endpoints()` and `open_endpoint_tree()`;
2. flat exact-name semantics and caller-enforced single-instance use;
3. one bindspace and one common declared stack per actor process;
4. separate parsed identity and local interface/netns realization;
5. explicit attach/open interface ownership, including the external
   lifetime precondition for attached links;
6. eager whole-table realization instead of a concurrent lazy cache;
7. explicit local WG config and routes, with no inferred secrets or
   host routing policy;
8. unchanged low-level root, nursery and `SpawnSpec` contracts;
9. privilege diagnostics with strict capability drop deferred to a
   helper architecture; and
10. the capable Linux environment required to claim dataplane proof.

Implementation begins only after this plan is reviewed and explicitly
approved.
