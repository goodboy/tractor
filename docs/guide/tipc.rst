TIPC: when the kernel does discovery
====================================

Every other ``tractor`` transport gives you a *pipe* and leaves
discovery to us: the registrar actor, the ``find_actor()``
round-trip, the whole :doc:`discovery` story. TIPC_
(Transparent Inter-Process Communication) is different — it's a
linux-kernel cluster protocol whose **service names live in a
cluster-wide name table the kernel itself maintains**.

Which flips the model:

- an actor's IPC address *is* a service name ``(stype,
  instance)`` — no host, no port,
- ``.bind()``-ing that name **is** service registration,
- a peer's ``.connect()``-by-name **is** the lookup, resolved
  and load-balanced in-kernel.

So for TIPC-capable deployments the registrar round-trip stops
being the only way peers find each other. Enable it per actor
like any other backend,

.. code:: python

    async with tractor.open_nursery(
        enable_transports=['tipc'],
    ) as an:
        ...

.. warning::

   TIPC is **opt-in and linux-only**. The ``tipc`` kernel module
   is not loaded on most boxes (``sudo modprobe tipc``), and the
   address family doesn't exist off-linux at all. Check
   :func:`tractor.ipc._tipc.is_tipc_available` before assuming;
   ``tractor`` never selects this backend for you.

.. _TIPC: https://en.wikipedia.org/wiki/Transparent_Inter-process_Communication

Your actor tree, in the kernel's name table
-------------------------------------------

The single best demo this backend has needs no ``tractor`` API
at all — boot a tree and ask ``tipc(8)`` what it sees:

.. code:: bash

    sudo modprobe tipc
    python examples/multihost/tipc_cluster/single_host.py

.. code:: text

    --- `tipc nametable show` :: root + 3 subactors ---
      Type       Lower      Upper      Scope    Port
      1953628160 1616       1616       cluster  3161982128
      1953628160 1219427151 1219427151 cluster  1587358717
      1953628160 2641339936 2641339936 cluster  1864021571
      1953628160 3344505866 3344505866 cluster  3816483388

    --- `tipc nametable show` :: after teardown (all withdrawn) ---
      Type       Lower      Upper      Scope    Port

Reading the rows,

- ``1953628160`` is ``0x74720000``, ``tractor``'s reserved
  service *type* — ascii ``tr`` in the high half, with the low
  16 bits free so an app can partition its own service classes
  via ``TIPCAddress._stype``,
- ``1616`` is the host-singleton registrar instance, the same
  "1616 is tractor's registrar" idiom as the TCP port and the
  ``registry@1616.sock`` UDS filename,
- the other three are per-actor instances derived from a
  ``blake2b`` digest of the actor's identity (see
  `Silent crosstalk`_),
- ``Scope`` is the address' :attr:`bindspace` — see `Scope is
  the bindspace`_.

Push-based discovery
--------------------

TIPC also exposes a *topology service*: subscribe and the kernel
pushes you name-table transitions as they happen.
:func:`tractor.ipc._tipc.open_topology_events` wraps it as an
``@acm`` yielding a ``trio`` receive-channel,

.. code:: python

    from tractor.ipc._tipc import open_topology_events

    async with open_topology_events() as events:
        async for ev in events:
            print(f'{ev.kind}: {ev.addr}')

.. code:: text

    watching the TIPC name table..
      [+] published  instance=1616         port=0x00000000:2375440573
    spawning subactors..
      [+] published  instance=186947472    port=0x00000000:3960753074
      [+] published  instance=2191362136   port=0x00000000:2263898853
    tearing down..
      [-] withdrawn  instance=186947472    port=0x00000000:3960753074

No polling, no registrar round-trip — this is the groundwork for
a registrar that keeps a live view of the actor set without ever
calling ``find_actor()``.

``filt`` picks the granularity: ``TIPC_SUB_SERVICE`` gives one
event per *name* becoming (un)available, ``TIPC_SUB_PORTS`` one
per *publisher* — which is what makes the duplicate-name case
below externally observable.

Scope is the bindspace
----------------------

Every ``tractor`` address type has a ``.bindspace`` — "the set
of hosts this bind is reachable from". For TCP that's the IP,
for UDS the socket-file directory. For TIPC it's the *scope*,
which is about as literal a reading of that docstring as exists:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - scope
     - meaning
   * - ``TIPC_NODE_SCOPE``
     - same host only — the UDS analogue
   * - ``TIPC_CLUSTER_SCOPE``
     - cluster-visible (the default)

``TIPC_ZONE_SCOPE`` is deprecated and aliased to cluster-scope
by modern kernels; ``tractor`` accepts it on input, folds it to
cluster and logs at ``transport`` level.

Spanning hosts
--------------

Single-host TIPC needs only ``modprobe``. Crossing hosts needs a
**bearer** enabled on both — an ethernet (L2) or UDP underlay
the kernel routes service names over:

.. code:: bash

    # on BOTH hosts
    sudo tipc bearer enable media eth device eth0
    # ..or, when L2 isn't available:
    sudo tipc bearer enable media udp name uc localip 10.0.11.1

    tipc link list      # must list the peer before you proceed

The two-host example pair then talks with **no IP, hostname or
port anywhere in either script** — both sides name the same
service and the kernel routes it. Move the server to a third
node and the client's dial keeps working, unchanged. See
``examples/multihost/tipc_cluster/`` for the full walkthrough
(that directory is excluded from CI precisely because it needs
real hardware).

.. note::

   TIPC over a UDP bearer composes with the WireGuard tunnel
   examples in ``examples/multihost/wg_lan/`` — cluster-wide
   kernel service discovery across an encrypted overlay.

Gotchas
-------

.. _Silent crosstalk:

**Silent crosstalk.** Unlike every other backend, a duplicate
bind does *not* raise ``EADDRINUSE``. TIPC accepts multiple
publishers of one name and **round-robins** connects between
them — verified: six dials alternated strictly between two
listeners. So an instance collision splits traffic silently
instead of erroring. That's why ``TIPCAddress.get_random()``
derives its instance from a ``blake2b`` digest of the actor
identity rather than a counter, and why two ``tractor`` trees
sharing both a cluster **and** an ``_stype`` share a namespace —
partition them with a distinct ``_stype``.

**Graceful close looks like a reset.** A peer closing cleanly
surfaces as ``BrokenResourceError``/``ECONNRESET`` rather than
the clean 0-byte EOF TCP and UDS give you. Benign — the
transport layer already classifies it as a normal disconnect —
but it does look alarming in ``transport``-level logs.

**Dialing an unpublished name** answers ``EHOSTUNREACH``
*instantly*, with no SYN-timeout wait. That's markedly better
discovery-ping behaviour than TCP; ``tractor`` normalizes it to
``ConnectionError`` so the usual lookup paths work unchanged.

**Multiaddrs are interim.** There's no registered ``/tipc``
protocol in the multiaddr table yet, so the grammar is
``str``-only:

.. code:: text

    /tipc/<stype>/<instance>/<scope>

Running the suite over TIPC
---------------------------

The backend is a first-class suite mode — the *entire* existing
test suite runs over it unmodified, which is the acceptance bar
for any ``tractor`` transport:

.. code:: bash

    sudo modprobe tipc
    pytest --tpt-proto tipc

Without the module that fails loudly and immediately with an
actionable message rather than a few hundred confusing connect
timeouts. Backend-specific unit tests live in
``tests/ipc/test_tipc.py`` and self-skip when the module is
absent.

Normative references
--------------------

The tipc.io documentation is stale in places. Treat the kernel
sources as the only authority:

- ``include/uapi/linux/tipc.h`` — address flavours, sockopts,
  the topology ``struct``\s
- ``net/tipc/socket.c``, ``net/tipc/topsrv.c``
- ``man 8 tipc``
