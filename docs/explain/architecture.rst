Anatomy of the runtime
======================

You can get a long way with ``tractor`` by treating it as "trio_
with nurseries that spawn processes". But once you start asking
*where does my msg actually go?*, *which process is that?* or
*who keeps the phonebook?*, it pays to know how the runtime hangs
together. This page walks the stack top to bottom.

.. d2:: diagrams/runtime_stack.d2
   :caption: The four runtime layers inside *every* actor process.
   :alt: layer cake of app tasks, tractor runtime, IPC, OS process
   :width: 70%

The layer cake
--------------

Every actor process is the same four-layer sandwich:

- **your app**: plain ``trio`` tasks, nurseries and cancel
  scopes; nothing special. ``tractor`` is a `structured
  concurrency`_ (SC) multi-processing runtime built on trio_ and
  the whole pitch is that this layer stays *just trio*: no
  callbacks, no futures, no proxy objects.
- **the** ``tractor`` **runtime**: a per-process
  :class:`tractor.Actor` running the msg loop and RPC task
  scheduler, plus the user-facing primitives layered on it:
  :class:`tractor.ActorNursery` (spawning + supervision),
  :class:`tractor.Portal` (calling into a peer) and
  :class:`tractor.Context` + :class:`tractor.MsgStream`
  (SC-linked cross-actor task pairs and streaming).
- **IPC channels**: one :class:`tractor.Channel` per connected
  peer, each wrapping a ``MsgTransport`` that ships
  msgspec_-typed msgs over TCP or UDS.
- **the OS**: one process per actor, started by a swappable
  spawn backend.

The property that holds it all together: SC composes *through*
the layers. A crash in a leaf actor's app task unwinds that
actor's trio tree, ships across its IPC channel as a typed
``Error`` msg, and unwinds the parent's trio tree in turn — the
"SC-transitive supervision protocol" from the README's pitch.
The whole tree cancels and errors like one big trio program; it
just happens to be spread across processes.

One actor, one process, one ``trio.run()``
------------------------------------------

A ``tractor`` "actor" is not a green thread, nor an object with
a mailbox, nor a coroutine: it's one OS process running one
:func:`trio.run` whose root task boots the runtime machinery —
msg loop, RPC task scheduler, IPC server — all embodied by a
single :class:`tractor.Actor` instance.

.. margin:: Shared nothing

   Processes buy you a real `shared nothing architecture`_: no
   accidentally-shared mutable state, no GIL contention, and
   every actor can be inspected (or killed) like any other OS
   process.

You rarely construct an :class:`~tractor.Actor` yourself; the
runtime makes exactly one per process and you grab it with
:func:`tractor.current_actor`:

.. code:: python

    import tractor

    actor = tractor.current_actor()  # NoRuntime if none running
    print(actor.aid.name)   # str name, need not be unique
    print(actor.aid.uuid)   # uuid4 str, IS unique
    print(actor.aid.pid)    # the OS pid
    print(actor.uid)        # legacy (name, uuid) pair

Identity is carried by the ``Aid`` msg-struct (see
``tractor.msg.types``): a ``name``/``uuid``/``pid`` triple
exchanged in the very first "mailbox handshake" whenever two
actors connect. It's what the registrar stores and what shows up
in logs and proc-titles. The older ``.uid`` 2-tuple of
``(name, uuid)`` predates ``Aid`` and is still pervasive across
the codebase; treat it as the legacy spelling of the same
identity.

If this smells like the `actor model`_, sure — but as the
README warns, it probably doesn't look like what you *think* an
actor model looks like, and that's intentional. Here an "actor"
is purely a runtime-unit-of-abstraction: process +
``trio.run()`` + IPC machinery.

IPC: channels, transports, addresses
------------------------------------

Two connected actors talk through a :class:`tractor.Channel`: a
duplex, per-peer msg pipe. Each ``Channel`` wraps a
``MsgTransport`` instance which does the wire work: framing,
encode/decode and the socket itself. The encoding is msgpack
(via msgspec_) and *every* msg is an instance of one of the
runtime's tagged-union :class:`msgspec.Struct` types: the
``Aid`` handshake, ``Start``/``StartAck`` (RPC init),
``Started``/``Yield``/``Stop``/``Return`` (the ctx dialog
phases), ``Error``, etc. There is no raw-bytes mode; the
msg-spec *is* the protocol, which is exactly what lets payloads
be type-limited per-context (see ``pld_spec`` in
:doc:`/guide/context`).

Addresses come in two spellings:

- *unwrapped*: the plain-tuple form you pass to user APIs —
  ``('127.0.0.1', 1616)`` for tcp, or a
  ``(<filedir>, <filename>)`` path-pair for uds;
- *wrapped*: the internal ``TCPAddress``/``UDSAddress`` struct
  types (plus libp2p-style multiaddr helpers over in
  ``tractor.discovery``).

You only ever need the tuple form; the runtime wraps and
unwraps at the boundaries.

TCP: the boring default
***********************

The default transport (``'tcp'``) binds each actor's IPC server
to loopback ``('127.0.0.1', <random port>)`` unless told
otherwise, and is the only choice when your tree spans hosts.
Nothing exotic: ``trio`` TCP streams + length-prefixed msgpack
framing.

UDS: same-host, creds included
******************************

Pass ``enable_transports=['uds']`` and actors instead talk over
unix-domain sockets, with socket files placed in the per-user
runtime dir (``$XDG_RUNTIME_DIR/tractor/`` on linux, the
``platformdirs`` equivalent elsewhere). Two perks over tcp on a
single host:

- no ports to fight over; addrs are just file paths,
- the kernel snitches on your peer for free: the listening side
  reads the connector's ``pid`` (plus ``uid``/``gid`` on linux)
  straight off the socket via ``SO_PEERCRED`` /
  ``LOCAL_PEERPID`` — no extra handshake msgs required B)

.. warning::

   Socket-file lifetime == listening actor lifetime. On
   listener teardown the runtime ``os.unlink()``\s the socket
   file immediately, so any *late* connection attempt (say, a
   sub-actor racing to deregister with a registrar that's
   already shutting down) fails with ``FileNotFoundError``.
   And ofc, UDS is same-host only.

Here's a full actor tree run entirely over uds:

.. literalinclude:: ../../examples/uds_transport_actor_tree.py
   :caption: examples/uds_transport_actor_tree.py
   :language: python

Picking a transport
*******************

Transport choice is per-actor via the ``enable_transports``
kwarg accepted by :func:`tractor.open_root_actor` (and proxied
through ``open_nursery()`` when it implicitly boots the
runtime), plus per-child via
``ActorNursery.start_actor(enable_transports=...)``. Two rules
the runtime enforces today:

- exactly ONE transport per actor: multi-transport actors are
  on the roadmap but currently raise ``RuntimeError``;
- your ``registry_addrs`` protos must all be in
  ``enable_transports``: mismatches fail fast with
  ``ValueError`` instead of (as in darker times) hanging the
  registrar handshake forever.

Spawn backends
--------------

How does an actor actually *become* a process? Via a swappable
spawn backend, selected with the ``start_method`` kwarg to
:func:`tractor.open_root_actor`:

``'trio'`` (default)
   The home-grown spawner: re-exec the child as
   ``python -m tractor._child`` using ``trio``'s subprocess
   machinery, then bootstrap it over the first IPC exchange
   (the parent ships a ``SpawnSpec`` msg carrying all init
   state). Supported on all platforms and the most battle
   tested choice by far.

``'mp_spawn'`` / ``'mp_forkserver'``
   The stdlib :mod:`multiprocessing` start-methods of the same
   names (forkserver is posix-only). Mostly interesting for
   ecosystem compat and start-up-latency tuning.

``'subint'`` (experimental, py3.14+)
   Each actor runs as a `PEP 734`_ sub-interpreter
   (``concurrent.interpreters``) driven on its own OS thread
   *inside the parent process*: interpreter-level
   shared-nothing isolation with much faster start-up. Yes,
   this bends the one-actor-one-process rule; the rest of the
   model is unchanged.

The ``TRACTOR_SPAWN_METHOD`` env-var beats any caller-passed
``start_method``, so you can swap backends under an unmodified
app:

.. code:: bash

    TRACTOR_SPAWN_METHOD=mp_forkserver python my_app.py

One current limitation worth knowing: ``debug_mode=True`` (the
crash-to-REPL machinery) is only supported on backends whose
child-side runtime is trio-native, e.g. the default ``'trio'``;
see :doc:`/guide/debugging` for the deats.

The registrar
-------------

Discovery needs a phonebook. Every actor, as part of boot,
registers its ``Aid`` and bind-addrs with the *registrar*: an
otherwise ordinary actor (a :class:`tractor.Registrar`, subtype
of :class:`~tractor.Actor`) that keeps the name -> addrs table
for the tree; on graceful exit each actor de-registers itself.

.. margin:: Default registry addrs

   With no ``registry_addrs`` passed:

   - tcp: ``('127.0.0.1', 1616)``
   - uds: ``registry@1616.sock``
     in the runtime dir

Who *is* the registrar? Decided at root boot, rendezvous style.
:func:`tractor.open_root_actor` probes each addr in
``registry_addrs`` with a quick connect-ping, then:

- **somebody answered**: this root is a plain actor; it
  registers with the existing registrar and binds random
  same-proto addrs for its own IPC server;
- **nobody answered**: this root *becomes* the registrar and
  binds the registry addrs itself.

So single-program trees need zero config — the root quietly
self-appoints — while multi-program setups share a registrar by
pointing every program at the same ``registry_addrs``. Pass
``ensure_registry=True`` to demand that *this* call create the
registry; it raises if the addrs are already served.

The lookup APIs — :func:`tractor.find_actor`,
:func:`tractor.wait_for_actor`, :func:`tractor.query_actor` and
:func:`tractor.get_registry` — all consult it (after first
checking already-connected peers):

.. literalinclude:: ../../examples/service_daemon_discovery.py
   :caption: examples/service_daemon_discovery.py
   :language: python

If you bump into "arbiter" in old issues or posts: that's the
legacy name for the same thing, surviving in-code only as the
``Arbiter = Registrar`` class alias; all current terminology is
"registrar"/"registry". Fair warning per the README: this is
still a **very naive** discovery sys (no re-election, no gossip
protocol... yet) and a registrar is expected to outlive its
registrants.

Runtime env vars
----------------

A few env-vars let you re-tune a whole tree *without touching
app code*; each wins over its corresponding kwarg:

.. list-table::
   :header-rows: 1
   :widths: 30 44 26

   * - env-var
     - effect
     - vs. kwarg
   * - ``TRACTOR_LOGLEVEL``
     - crank (or silence) console-log verbosity for every actor
       in the tree
     - beats ``loglevel``
   * - ``TRACTOR_SPAWN_METHOD``
     - swap the process spawn backend
     - beats ``start_method``
   * - ``TRACTOR_ENABLE_STACKSCOPE``
     - install the ``SIGUSR1`` task-tree-dump handler in every
       actor, even outside ``debug_mode`` (see
       :doc:`/guide/debugging`)
     - OR'd with ``enable_stack_on_sig``

Spotting actors from your shell
*******************************

Every sub-actor sets an OS-level proc-title of the form
``_subactor[<name>@<pid>]`` (via ``setproctitle``, silently
skipped when not installed) so ``ps``/``htop``/``pstree`` show
*which actor is which* at a glance. The README's signature
incantation — watch a tree build and self-destruct live:

.. code:: bash

    $TERM -e watch -n 0.1 "pstree -a $$" \
        & python examples/nested_actor_tree.py \
        && kill $!

For scripting there are two stable cmdline markers:

.. code:: bash

    pgrep -fa '_subactor\['     # live, titled sub-actors
    pgrep -fa 'tractor._child'  # 'trio'-backend children not
                                # yet (re)titled

The title also lands in the kernel ``comm`` (truncated to ~15
bytes) which survives into zombie state — that's what
``tractor``'s own test-harness reapers key off. To be crystal
clear about the contract though: you should never *need* a
reaper; if you can create zombie child processes (without using
a system signal) it **is a bug** — please report it!

Logging
-------

The runtime logs through a thin adapter over stdlib
:mod:`logging` that stamps every record with actor + task info.
Two calls get you going:

.. code:: python

    from tractor.log import get_console_log, get_logger

    log = get_logger(__name__)  # actor/task-aware sub-logger
    get_console_log('info')     # attach console handler @ level

(or just pass ``loglevel='info'`` to
:func:`tractor.open_root_actor` and the console handler comes up
with the runtime).

``tractor`` adds custom levels — and matching logger methods —
that slot between the stdlib ones so you can dial in *which
runtime subsystem* you want to hear from: ``.transport()`` (5),
``.runtime()`` (15), ``.devx()`` (17), ``.cancel()`` (22), plus
a ``PDB`` (500) level for debugger chatter. E.g.
``loglevel='cancel'`` plays the whole cancellation chorus while
staying quiet about transport-layer noise. Beyond that
``tractor`` isn't opinionated about how you consume logs: it's
all stdlib ``logging`` underneath.

Where to next?
--------------

.. seealso::

   - :doc:`/guide/context` — the SC-linked cross-actor task API
     that rides on every ``Channel``.
   - :doc:`/guide/debugging` — ``debug_mode``, the multi-process
     REPL and ``stackscope`` task-tree dumps.
   - :doc:`/explain/sc-distributed` — *why*
     one-actor-one-process, and what kind of "actor model" this
     is (and isn't).

.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _trio: https://github.com/python-trio/trio
.. _actor model: https://en.wikipedia.org/wiki/Actor_model
.. _shared nothing architecture: https://en.wikipedia.org/wiki/Shared-nothing_architecture
.. _msgspec: https://jcristharif.com/msgspec/
.. _PEP 734: https://peps.python.org/pep-0734/
