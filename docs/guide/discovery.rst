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
   by the full ``(name, uuid)`` pair. Name-based lookups simply
   resolve to the *last* registered match, so if you boot five
   actors all named ``'bob'``, you get the freshest ``'bob'`` B)

First boot: who's the registrar?
--------------------------------

By default the **root actor** *is* the registrar; subactors
inherit the tree's ``registry_addrs`` at spawn time so the whole
clan shares one registry with zero config on your part.

The bootstrap rule inside ``open_root_actor()`` is delightfully
simple:

- on boot, ping every socket addr in ``registry_addrs``; when none
  are passed the per-transport defaults are used: for TCP the
  loopback ``('127.0.0.1', 1616)``, for UDS a
  ``registry@1616.sock`` file,

- if a registrar answers, you boot as a plain (non-registrar) root
  actor and register with the *existing* registry; your own IPC
  server binds random same-transport addrs instead,

- if **nothing answers, congratulations: you just became the
  registrar**. Your transport server binds the registry addrs
  themselves and you start serving lookups for everyone else.

Pass ``ensure_registry=True`` when your program *requires* being
the one-and-only registrar; boot then fails loudly with a
``RuntimeError`` if some other process already bound the registry
socket(s).

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

- ``only_first=False``: deliver a ``list[Portal]`` of *all*
  matches found across the queried registrars instead of just the
  first,

- ``raise_on_none=True``: raise a ``RuntimeError`` instead of
  yielding ``None`` when no match is found — for when absence is
  a hard error in your app.

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

Before doing any RPC to the registrar, every lookup first scans
the calling actor's *already-connected peers*: if you have a live
channel to an actor named ``name`` you get a portal over it
immediately, no registrar round-trip at all.

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

Per the bootstrap rules above, if the registrar at those addrs is
*not* reachable this process simply becomes its own (registrar)
root — so the same code works standalone and as a tree-joiner.

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
process-tree-local registrar holding a dict, no replication, no
re-election when it dies, no cross-host propagation. That's
intentional (for now); it covers the "wire up my services on this
host" case without dragging in a consensus protocol.

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
.. _modern protocol: https://en.wikipedia.org/wiki/Rendezvous_protocol
.. _discovery: https://zguide.zeromq.org/docs/chapter8/#Discovery
.. _#216: https://github.com/goodboy/tractor/issues/216
