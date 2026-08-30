Actor discovery
===============

So you've spawned a tree of trio-"actors"; now their tasks need to
*find* each other to start a dialog. ``tractor`` ships a (self
admittedly) **very naive** discovery system which is nonetheless
mighty handy for wiring up service-style apps: a built-in
*registrar* actor plus a small set of lookup APIs that deliver
a live, connected ``Portal`` to whichever peer you're after.

.. d2:: diagrams/actor_tree.d2
   :margin:
   :caption: The root actor doubles as the *registrar* by
       default; every spawned actor registers itself with it.
   :alt: actor tree with root acting as registrar

Because ``tractor`` is built on structured concurrency (SC), the
discovery layer is *not* some external etcd/consul-shaped service
you have to babysit; it's just another actor — normally the root
of your tree — doing a bit of bookkeeping as part of the runtime.

Every actor phones home
-----------------------

On runtime boot **every** actor self-registers with the registrar:
it submits its unique ``(name, uuid)`` identity pair (aka its
``uid``) mapped to the list of transport addresses its IPC server
is bound to. On graceful teardown it likewise *un*-registers, so
the registry tracks the live tree as it grows and shrinks.

.. note::
   Actor names are **not** enforced unique — the registry is keyed
   by the full ``(name, uuid)`` pair. A name lookup returns one
   matching registration, but the API does not promise which match
   wins. Use unique service names when selection matters.

First boot: who's the registrar?
--------------------------------

By default the **root actor** *is* the registrar; subactors
inherit the tree's ``registry_addrs`` at spawn time so the whole
clan shares one registry with zero config on your part.

The bootstrap rule inside ``open_root_actor()`` is delightfully
simple:

- on boot, probe every addr in ``registry_addrs`` with a bounded
  Tractor ``Aid`` handshake; when none are passed the per-transport
  defaults are used: for TCP the
  loopback ``('127.0.0.1', 1616)``, for UDS a
  ``registry@1616.sock`` file,

- if a registrar answers, you boot as a plain (non-registrar) root
  actor and register with the *existing* registry; your own IPC
  server binds random same-transport addrs instead,

- if every address is absent, congratulations: you just became the
  registrar. Your transport server binds the registry addrs
  themselves and you start serving lookups for everyone else,
- if no registrar answers but an address is occupied by a foreign or
  non-responsive endpoint, startup fails instead of binding over it.

Pass ``ensure_registry=True`` when your program *requires* being
the one-and-only registrar; boot then fails loudly with a
``RuntimeError`` if some other process already bound the registry
socket(s).

A dedicated registrar
---------------------
That second rule — *"if a registrar answers, boot as a plain
root"* — is all you need to run the registry as its own
**standalone process**, decoupled from any app tree's root. In the
daemon process, enter ``open_root_actor()`` with an explicit
``registry_addrs`` and ``ensure_registry=True``; the latter makes
startup fail instead of silently joining a registrar that won the
address. Point each app tree at the address that daemon actually
bound:

.. literalinclude:: ../../examples/discovery/dedicated_registrar.py
   :caption: examples/discovery/dedicated_registrar.py
   :language: python

The example's selector socket binds but deliberately never listens.
It owns the kernel-selected local address only long enough to read it,
then closes so Tractor's actual listener can bind the same address.
This is not a socket transfer: the close/rebind handoff is non-atomic,
so the example retries with a fresh candidate only when registrar
startup reports that another process claimed the released address.
Retries are bounded, and other startup failures remain visible. It
publishes the selected address only after the actor context enters.
It also performs the lookup inside a separate ``client`` actor. The
service is its sibling, not its child, so the client has no spawn-time
service channel to satisfy the local-peer fast path. The
``query_actor()`` assertion verifies that a registrar portal handled
the lookup before ``find_actor()`` makes the service RPC.

This is the "registrar as a subsystem, not the app root actor"
shape. Two caveats today (both tracked as #472 follow-ups):
``enable_transports`` is single-proto per runtime, so a registrar
can't yet serve multiple backends at once; and there's no way to
spawn a registrar as a *sub*-actor of a shared tree (only as its
own root), since ``start_actor()`` has no custom-``actor_cls``
hook.

Looking up actors
-----------------

All lookup APIs are async context managers, so the SC rule you
already know from the rest of ``tractor`` holds here too: any
delivered portal (and its underlying IPC channel) is scoped to
your ``async with`` block — no dangling connections.

``find_actor()``
****************

The workhorse: ask the registrar for ``name`` and connect a portal
to the match, or get ``None`` back when nobody's home:

.. code:: python

    async with tractor.find_actor('data_feed') as portal:
        if portal is None:
            ...  # not registered anywhere; maybe spawn it?
        else:
            await portal.run(do_stuff)

Knobs worth knowing:

- ``registry_addrs=[...]``: query specific (possibly multiple,
  possibly remote) registrars instead of your tree's default,

- ``only_first=True``: after all configured registrars are queried
  concurrently, yield the result in the first ``registry_addrs``
  position. This is configured order, not first-reachable order, so
  the result can be ``None`` even when a later registrar returned a
  portal,

- ``only_first=False``: when any query succeeds, yield an ordered
  ``list[Portal | None]`` with one result per ``registry_addrs``
  position; misses remain ``None`` placeholders. When every query
  misses, yield ``None`` instead of a list. This does not enumerate
  every duplicate name in one registrar,

- ``raise_on_none=True``: raise a ``RuntimeError`` when every
  registrar query returns ``None``. With ``only_first=True`` it does
  not raise merely because the first ordered result is ``None`` when
  a later result is a portal.

``wait_for_actor()``
********************

Blocks until *someone* registers under ``name``, then yields a
portal to that registree. Perfect for "wait for my sibling service
to come up" sequencing:

.. code:: python

    async with tractor.wait_for_actor('service') as portal:
        await portal.run(some_fn)

``query_actor()``
*****************

A lookup *without* connecting to the target: yields an
``(addr, reg_portal)`` pair where ``addr`` is the peer's preferred
transport address, or ``None`` when nothing is registered under
that name. Use it for liveness peeks or to log where a service
lives without actually dialing it up.

``get_registry()``
******************

Yields a portal straight to the registrar actor itself — or a
``LocalPortal`` shim when the calling actor *is* the registrar
(no IPC required to talk to yourself, hopefully).

Fast paths and address preference
---------------------------------

Before doing any RPC to the registrar, ``query_actor()``,
``wait_for_actor()``, and the default ``find_actor()`` lookup first
scan the calling actor's *already-connected peers*. If the caller
has a live channel to an actor named ``name``, it gets a portal over
that channel immediately, with no registrar round-trip.

When a registry entry holds *multiple* addresses (a multihomed
actor) the "best" one is chosen by locality:

1. UDS — same-host guaranteed, lowest overhead,

2. local TCP — loopback or any of this host's own interface
   addrs,

3. remote TCP — the only option when actually distributed.

Within a tier the most recently registered addr wins. Stale
entries (an addr that no longer accepts connections) are detected
on use and deleted from the registrar's table on your behalf.

Demo: register and find a service
---------------------------------

The simplest possible spin: start a subactor, ask the registrar
where it lives, and wait on its registration:

.. literalinclude:: ../../examples/service_discovery.py
   :caption: examples/service_discovery.py
   :language: python

The daemon-service pattern
--------------------------

The classic deployment shape: a long-lived daemon actor serves
RPC, later-running code discovers it by name, calls in, and
gracefully cancels it when the job is done:

.. literalinclude:: ../../examples/service_daemon_discovery.py
   :caption: examples/service_daemon_discovery.py
   :language: python

Note the teardown ordering — *graceful cancel* of the daemon via
its portal is part of the pattern; under SC a "service" is still
somebody's child and somebody is responsible for reaping it.

Joining an existing tree from outside
-------------------------------------

Discovery isn't limited to a single program: any standalone script
can join a running tree by booting its *own* root actor pointed at
the existing registrar:

.. code:: python

    import trio
    import tractor

    async def main():
        async with (
            # contact the live tree's registrar
            tractor.open_root_actor(
                registry_addrs=[('127.0.0.1', 1616)],
            ),
            tractor.find_actor('data_feed') as portal,
        ):
            ...  # RPC away like you were born here

    trio.run(main)

Per the bootstrap rules above, if those addrs are absent this process
becomes its own registrar root, so the same code works standalone and
as a tree-joiner. An occupied address that does not complete a
Tractor registrar handshake fails startup instead of being rebound.

"Arbiter"? A legacy naming note
-------------------------------

In older releases (and many an old blog post or issue thread) the
registrar actor was called the *arbiter*, with matching APIs like
``get_arbiter()`` and an ``arbiter_addr`` argument. All of that
terminology is retired: it's *registrar*/*registry* everywhere now
(``registry_addrs``, ``get_registry()``, ...) and the
``tractor.Arbiter`` export survives only as a back-compat alias of
``tractor.Registrar``. If you see "arbiter" somewhere, mentally
substitute "registrar" and you're up to date.

.. note::
   Multihoming nerds: ``tractor.discovery`` also ships
   libp2p-style *multiaddr* helpers — ``mk_maddr()`` and
   ``parse_maddr()`` — for describing transport endpoints as
   structured strings.

Very naive, very honest
-----------------------

To be clear, this is a **very naive** discovery system: one
in-memory registrar holding a dict, no replication, no re-election
when it dies, and no automatic cross-host propagation. Separate
programs can use the same reachable registrar, as above, but must be
configured with its address. That's intentional (for now); it covers
the "wire up my services" case without a consensus protocol.

On the roadmap (issue `#216`_ tracks a chunk of it):

- registrar high(er)-availability: staying up past tree teardown
  and re-election,

- a `gossip protocol`_ for decentralized cross-host discovery (the
  zguide's `discovery`_ chapter is the spiritual reference),

- `modern protocol`_ (rendezvous) style meet-up points.

If any of that scratches your itch, the issue tracker would love
to hear from you.

.. seealso::
   - :doc:`/guide/testing` — watching live actor trees (and their
     registrar) while the test suite or your app runs.
   - API refs: :func:`tractor.find_actor`,
     :func:`tractor.wait_for_actor`,
     :func:`tractor.query_actor`,
     :func:`tractor.get_registry`,
     :class:`tractor.Registrar`.

.. _gossip protocol: https://en.wikipedia.org/wiki/Gossip_protocol
.. _modern protocol:
   https://en.wikipedia.org/wiki/Rendezvous_protocol
.. _discovery: https://zguide.zeromq.org/docs/chapter8/#Discovery
.. _#216: https://github.com/goodboy/tractor/issues/216
