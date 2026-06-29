Higher-level cluster APIs
=========================

Sometimes you don't want a hand-crafted supervision tree; you want
"a pile of workers, one per core, now please". For that there's
:func:`tractor.open_actor_cluster`: a convenience wrapper which
spawns a *flat* cluster of subactors and hands you back a portal to
each,

.. code:: python

    @acm
    async def open_actor_cluster(
        modules: list[str],            # RPC allowlist for workers
        count: int = cpu_count(),      # one per core by default
        names: list[str]|None = None,  # default: 'worker_{i}'
        hard_kill: bool = False,       # fwd to `an.cancel()`
        **runtime_kwargs,              # fwd to `open_root_actor()`
    ) -> AsyncGenerator[dict[str, tractor.Portal], None]:

A cluster in one block
----------------------

.. literalinclude:: ../../examples/quick_cluster.py
   :caption: examples/quick_cluster.py
   :language: python

Walkthrough,

- ``open_actor_cluster(modules=[__name__])`` concurrently spawns
  one subactor per detected core (per
  :func:`multiprocessing.cpu_count`); the ``modules`` list is the
  usual ``enable_modules``-style capability allowlist so workers
  may run functions defined in this module,
- it yields a ``dict[str, tractor.Portal]`` mapping worker name to
  portal; note the keys get prefixed with the *spawning* actor's
  name, so from the root you'll see ``'root.worker_0'``,
  ``'root.worker_1'``, etc.,
- a plain :class:`trio.Nursery` then fans out one
  ``portal.run(sleepy_jane)`` per worker; each prints its actor
  ``.uid`` from inside its own process then naps forever — what
  runs *inside* each worker (and how many tasks you point at it)
  is entirely yours to compose,
- ``tractor.trionics.collapse_eg()`` un-nests the strict
  ``ExceptionGroup`` wrapping so the demo's ``KeyboardInterrupt``
  surfaces as itself instead of arriving eg-boxed,
- on block exit the whole fleet is torn down for you via
  :meth:`tractor.ActorNursery.cancel`; pass ``hard_kill=True`` at
  open time to skip straight to OS-level termination instead of
  the graceful ladder described in :doc:`/guide/cancellation`.

Sizing, naming, fleet-wide options
----------------------------------

``count`` doesn't have to be core-count and the auto-generated
``'worker_{i}'`` names are just the default; pass your own (the
length must match ``count`` or you get a ``ValueError``). Any
extra ``**runtime_kwargs`` pass through verbatim to
:func:`tractor.open_root_actor`, so fleet-wide runtime options are
one kwarg away,

.. code:: python

    async with tractor.open_actor_cluster(
        modules=['mylib.workers'],
        count=4,
        names=['scout', 'miner', 'smelter', 'smith'],
        debug_mode=True,    # whole-fleet crash-to-REPL
    ) as portal_map:
        ...

From here the composition patterns are the usual ``tractor`` fare:
``portal.run()`` for one-shot calls (as in the demo), or — for a
persistent bidirectional dialog per worker — concurrently enter N
``portal.open_context()`` blocks with
``tractor.trionics.gather_contexts()``; see :doc:`/guide/context`
for that whole layer.

Clusters vs. nurseries
----------------------

.. d2:: diagrams/actor_tree.d2
   :margin:
   :caption: The general shape: arbitrary nesting. A cluster is
       this, minus the nesting.
   :alt: a nested supervision tree of subactors

``open_actor_cluster()`` is sugar, not a new primitive: under the
hood it's just :func:`tractor.open_nursery` plus N concurrent
``start_actor()`` calls plus a ``.cancel()`` on the way out. Reach
for it when,

- you want a *flat*, homogeneous fleet (classic worker-pool or
  map-style fan-out shapes),
- "one per core" — or a fixed ``count`` — is the right sizing
  story,
- every child can share the same spawn options.

Drop down to a raw :class:`tractor.ActorNursery` when the topology
gets any fancier: nested trees, heterogeneous children, per-child
``debug_mode``/transport/module options, daemons mixed with
one-shot workers, and so on (see :doc:`/guide/parallelism` for a
hand-rolled pool). Either way the supervision semantics are
identical: one-cancels-all error propagation and the no-zombies
guarantee from :doc:`/guide/cancellation` apply to clusters too.

Provisional, by design
----------------------

.. note::

   APIs in this section are considered **provisional**: the
   signature and semantics of :func:`tractor.open_actor_cluster`
   may shift as higher-level supervision machinery lands. We
   encourage you to try it and provide feedback — the
   `matrix channel`_ is the place to say hi, and `#22`_ tracks the
   broader supervisor-strategy roadmap.

.. seealso::

   - :doc:`/guide/parallelism` — worker pools built "by hand" with
     plain actor nurseries (and why that's easy peasy),
   - :doc:`/guide/cancellation` — the teardown machinery a cluster
     inherits for free.

.. _matrix channel: https://matrix.to/#/!tractor:matrix.org
.. _#22: https://github.com/goodboy/tractor/issues/22
