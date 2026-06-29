Parallelism and worker pools
============================

The initial ask is almost always the same: *"how do i make a worker
pool?"* — i.e. the thing :mod:`multiprocessing` and
:class:`concurrent.futures.ProcessPoolExecutor` get reached for
once the GIL becomes the enemy.

Here's the structured concurrency (SC) answer: ``tractor`` is built
to handle any SC process tree you can imagine; a "worker pool"
pattern is a trivial special case. So instead of shipping a pool
*class* with knobs bolted on, you compose one from the same two
ingredients used everywhere else in ``tractor``: an actor nursery
and some IPC.

The stdlib baseline
-------------------

For a fair comparison, start from the canonical
:class:`~concurrent.futures.ProcessPoolExecutor` primes example
straight out of the Python docs,

.. literalinclude:: ../../examples/parallelism/concurrent_futures_primes.py
   :caption: examples/parallelism/concurrent_futures_primes.py
   :language: python

Synchronous code, a hidden thread + IPC machine under the hood, and
an API surface (executors, futures, ``.map()``) invented to paper
over the fact that the pool isn't part of your program's task tree.
Keep an eye on three things for the rewrite: how work is submitted,
how results come back, and what happens when a worker dies.

The ``tractor`` way
-------------------

Now the same workload as a ``tractor`` program,

.. literalinclude:: ../../examples/parallelism/concurrent_actors_primes.py
   :caption: examples/parallelism/concurrent_actors_primes.py
   :language: python

What's different (and what isn't),

- ``worker_pool()`` is ~30 lines of *your* code: an actor nursery
  spawning ``workers`` subactors — each a full process running its
  own ``trio`` task tree — kept alive and ready for work until the
  block exits; ``enable_modules=[__name__]`` is the capability
  allowlist letting them run this module's functions,
- jobs are "submitted" by just... calling the function:
  ``portal.run(is_prime, n=value)`` runs ``is_prime()`` in a
  worker and hands back its result like any local ``await``,
- results stream back through a plain
  :func:`trio.open_memory_channel` *as they complete* — no futures
  and no polling,
- teardown is one ``await tn.cancel()``
  (:meth:`tractor.ActorNursery.cancel`), and any worker crash
  triggers the one-cancels-all machinery from
  :doc:`/guide/cancellation` — a dead worker can never strand the
  pool.

This uses no extra threads, fancy semaphores or futures; all we
need is ``tractor``'s IPC! The full scorecard,

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - ``concurrent.futures``
     - ``tractor``
   * - ``ProcessPoolExecutor()``
     - ``worker_pool()`` — yours, ~30 lines
   * - ``executor.map(is_prime, PRIMES)``
     - ``actor_map(is_prime, PRIMES)`` async-gen
   * - ``Future`` + internal result queue
     - :func:`trio.open_memory_channel`
   * - results in input order
     - results as they complete
   * - worker crash -> ``BrokenProcessPool``
     - boxed :class:`tractor.RemoteActorError`
   * - pool teardown on ``with`` exit
     - one-cancels-all nursery teardown

.. margin:: How many workers?

   Same calculus as any process pool: about core-count for
   CPU-bound work (the default sizing in
   :doc:`/guide/clustering`); more only if workers block on I/O —
   though at that point you likely want plain ``trio`` tasks, not
   processes.

And because the pool is just SC code, every variation — bounded
submission, per-worker state, streaming partial results (see
:doc:`/guide/streaming`), nested pools — is a local edit to your
pool, not a feature request against an executor class B)

An *async* pool, though?
************************

Yep: RPC targets must be async functions — the runtime rejects a
plain ``def`` with ``TypeError: ... must be an async function!``.
That's not zealotry, it's cancel-responsiveness: each worker is a
full ``trio`` runtime whose msg loop is what hears graceful cancel
requests, and a hot loop that never yields can't be (politely)
interrupted.

Two practical consequences,

- CPU-bound loops should checkpoint once in a while; note how
  ``burn_cpu()`` in the next example sprinkles ``await
  trio.sleep()`` calls so the worker stays responsive while still
  pegging a core,
- if some sync call blocks a worker anyway you're still covered:
  an unresponsive actor just rides the graceful-then-hard teardown
  ladder from :doc:`/guide/cancellation` instead of acking its
  cancel — slower, but never a zombie.

Run a func in a process
-----------------------

Even a pool can be overkill; "run this one async func in a
subprocess and give me the result" is a one-liner via
:meth:`tractor.ActorNursery.run_in_actor`,

.. literalinclude:: ../../examples/parallelism/single_func.py
   :caption: examples/parallelism/single_func.py
   :language: python

``run_in_actor()`` is a *convenience wrapper* — spawn an actor, run
exactly one task in it, reap on result — not the core spawning
model (that's :meth:`tractor.ActorNursery.start_actor` plus
:meth:`tractor.Portal.open_context`; see :doc:`/guide/context`).
But for this fire-and-collect shape it's exactly the right amount
of typing.

As the module docstring suggests, run it under a process-tree
monitor to watch the child appear and get reaped,

.. code:: bash

    $TERM -e watch -n 0.1  "pstree -a $$" \
        & python examples/parallelism/single_func.py \
        && kill $!

You'll see a core get burned in both parent and child — real
parallelism, no GIL sharing, since these are processes (i.e.
*non-shared-memory threads*).

When all you have is sync code
------------------------------

Honesty corner: if your workload is purely *synchronous* functions
and you've zero need for IPC dialogs, streaming, daemons or
supervision trees — i.e. you really do just want
"``ProcessPoolExecutor`` but ``trio``-native" — the smaller,
focused `trio-parallel`_ project may serve you better. ``tractor``
happily covers the use case (as above) but brings a whole runtime
along for the ride. (And when blocking I/O — not the GIL — is the
actual problem, plain in-process :func:`trio.to_thread.run_sync`
may be all you ever needed.)

And to *see* that runtime's process-management story — a per-core
fleet self-destructing with zero zombies left behind — go run
``examples/parallelism/we_are_processes.py``, walked through in
the :doc:`/start/quickstart`.

.. seealso::

   - :doc:`/guide/clustering` — the one-liner flat-cluster
     convenience (``open_actor_cluster()``) for when even a
     hand-rolled pool is too much typing,
   - :doc:`/guide/cancellation` — why pool teardown is bulletproof
     (graceful-then-hard escalation, no zombies),
   - :doc:`/guide/context` — the core per-task API your pool
     workers can graduate to.

.. _trio-parallel: https://github.com/richardsheridan/trio-parallel
