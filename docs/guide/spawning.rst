Spawning actors
===============
If you know trio_ you know the drill: you don't get to launch
a task off into the void, you open a nursery_, the nursery owns
the task, and the block can't exit until every child is done.
That discipline is `structured concurrency`_ (SC) — see the
seminal `blog post`_ if you haven't yet — and it's the whole
religion around here.

``tractor`` applies that exact discipline to **processes**: an
:class:`~tractor.ActorNursery` is a *process nursery*. Every
"task" it starts is a fresh Python process running its own
``trio.run()``-scheduled task tree; we call each one a
``trio``-"*actor*". Parents must wait on (and clean up after)
their children, transitively, all the way down the tree.

.. d2:: diagrams/actor_tree.d2
   :caption: A process tree of ``trio``-task-trees.
   :alt: a nested actor tree where every parent supervises its children

Though a "process nursery" differs in complexity (and slightly
in semantics) from a single-threaded task nursery, most of the
interface is the same. The main difference is that each spawned
child contains a full, *parallel-executing* ``trio`` task tree.
The following super powers ensue:

- tasks started in a child actor are completely independent of
  tasks started in the current process; they execute in
  **parallel** and are scheduled by their own actor's ``trio``
  run loop.
- tasks scheduled in a remote process still maintain an SC
  protocol *across memory boundaries* using a so called
  "SC dialogue protocol" which keeps task-hierarchy lifetimes
  linked across the IPC layer.
- a remote task can fail and have that failure relayed back to
  the caller task (living in some other actor) as a serialized
  :exc:`~tractor.RemoteActorError`; no spawned process or RPC
  task can ever just go off on its own.

Opening a (process) nursery
---------------------------
:func:`tractor.open_nursery` is the entrypoint:

.. code:: python

    async def main():
        async with tractor.open_nursery() as an:
            ...  # spawn some actors B)

    trio.run(main)

Notice there's no runtime-boot ceremony: if no actor runtime is
up yet (i.e. you're in a plain old Python process),
``open_nursery()`` *implicitly* enters
:func:`tractor.open_root_actor` for you, making this process the
**root actor** of a new tree. Any extra keyword args you pass
are proxied straight through to ``open_root_actor()``, so the
runtime config lives wherever you open your first nursery:

.. code:: python

    async with tractor.open_nursery(
        loglevel='info',
        debug_mode=True,  # crash-to-REPL for the whole tree
    ) as an:
        ...

If you want the runtime up *without* spawning anything (or you
prefer the config to be loudly explicit) enter
``open_root_actor()`` yourself first; the nursery will detect
the running runtime and skip the implicit boot. Either way,
nesting a second root inside an existing tree is an error.

Inside a *subactor* the same call just works: any actor may open
nurseries of its own, which is how you get arbitrarily deep
trees (more on that below).

``start_actor()``: daemons that live until cancelled
----------------------------------------------------
:meth:`~tractor.ActorNursery.start_actor` is **the** core
spawning primitive. It starts a *daemon* actor: a process with
no designated "main task" besides the runtime itself. It boots,
registers with its parent, and then sits there serving RPC
requests until somebody cancels it. You get back a
:class:`~tractor.Portal` for doing exactly that kind of
somebody-ing:

.. literalinclude:: ../../examples/actor_spawning_and_causality_with_daemon.py
   :caption: examples/actor_spawning_and_causality_with_daemon.py
   :language: python

What's going on here?

- ``start_actor('frank', enable_modules=[__name__])`` forks off
  a new process, boots a ``tractor`` runtime inside it, and
  allows it to serve functions from the current module (see the
  allowlist section below).
- each ``await portal.run(...)`` schedules a *new* task in
  frank's task tree and waits on its result — the full RPC story
  lives in :doc:`/guide/rpc`.
- frank has no main task to complete, so without the final
  ``await portal.cancel_actor()`` the nursery block would wait
  on him **forever**. Daemon lifetimes are *yours* to end; that
  explicitness is the point.

``run_in_actor()``: quick one-shot parallelism
----------------------------------------------
:meth:`~tractor.ActorNursery.run_in_actor` is the convenience
wrapper: spawn an actor, run exactly one async function in it,
then reap the process as soon as the result arrives.

.. code:: python

    async with tractor.open_nursery() as an:
        portal = await an.run_in_actor(burn_cpu)
        # burn rubber in the parent too...
        await burn_cpu()
        total = await portal.wait_for_result()

A few details worth knowing:

- the actor is named after the function unless you pass
  ``name='something_cuter'``.
- the function's module is auto-added to the child's
  ``enable_modules`` allowlist.
- extra ``**kwargs`` are forwarded to the function itself.
- the child is *auto-cancelled* once its "main" result lands;
  at nursery exit these run-once children are always reaped
  first (causality_ is paramount!).

.. note::

   ``run_in_actor()`` is a convenience, **not** the core model.
   The source literally marks it for an eventual rebuild as
   a thin "hilevel" wrapper on top of
   :meth:`~tractor.Portal.open_context` (the modern inter-actor
   task API). Teach your fingers to use it for quick
   fire-and-collect parallelism — think a per-function
   trio-parallel_ style one-shot — and reach for
   ``start_actor()`` + ``open_context()`` for anything
   long-lived, stateful or streaming
   (:doc:`/guide/context`).

Actor lifetimes and teardown order
----------------------------------
So we have two lifetime flavors:

- **run-once** (``run_in_actor()``): lives exactly as long as
  its single task; reaped the moment its result (or error)
  arrives.
- **daemon** (``start_actor()``): lives until *someone* cancels
  it — an explicit ``await portal.cancel_actor()``, a bulk
  ``await an.cancel()``, or the one-cancels-all strategy kicking
  in on error.

On a clean exit of the nursery block the teardown order is:

1. the nursery waits on every run-once actor's final result;
   any errors from these are raised immediately so your code
   (acting as supervisor) gets first crack at handling them.
2. then it waits on daemon actors — **indefinitely**. If you
   spawned a daemon, you own its lifetime.

When a child *is* cancelled, teardown is graceful-first per SC
discipline: the runtime sends an IPC cancel request and gives
the child a bounded window to ack; only when a child is too
slow does the nursery escalate to an OS-level hard kill of the
process. There is no path where a child is silently left
running:

    ``tractor`` tries to protect you from zombies, no matter
    what. If you can create zombie child processes (without
    using a system signal) it **is a bug**.

Per-process cleanup hooks
*************************
Need something torn down when an actor's runtime exits, no
matter how it exits? Every actor carries
a process-global :class:`contextlib.ExitStack` at
``Actor.lifetime_stack`` which is closed at the very end of
runtime teardown:

.. code:: python

    db = await connect_db()
    tractor.current_actor().lifetime_stack.callback(db.close)

(A so-far under-advertised api — expect it to get more love.)

When things blow up: one-cancels-all
------------------------------------
The default (and currently only) supervision strategy is the
same one ``trio`` nurseries use: **one-cancels-all**. If your
nursery-block body errors, every child actor is cancelled. If
a child errors, the failure is relayed to the nursery as a
boxed :exc:`~tractor.RemoteActorError` (original type preserved
via ``.boxed_type``), all *other* children are cancelled, and
the error(s) re-raise locally — exactly like ``trio``, just
process-wide. Erlang-style alternative strategies are a long
standing roadmap item.

The full story — how cancel requests relay across the tree, who
``.canceller`` was, debugging mid-teardown — lives in
:doc:`/guide/cancellation`.

The module allowlist: ``enable_modules``
----------------------------------------
A subactor will only serve functions from modules its parent
*explicitly* enabled at spawn time:

.. code:: python

    portal = await an.start_actor(
        'service',
        enable_modules=['mypkg.service'],  # or [__name__]
    )

At child boot the runtime imports each listed module so inbound
RPC requests can resolve function references against it. Ask
a peer to run something from any *other* module and you get an
:exc:`~tractor.ModuleNotExposed` error relayed back — the child
never even looks the function up.

Think of it as the first, deliberately coarse layer of
capability-style permissioning: if you don't hand an actor
a module, no peer can invoke anything inside it. (Finer-grained
capability-based messaging protocols are on the roadmap.)

The ``enable_modules=[__name__]`` idiom — "let the child run
functions from the *current* module" — is what you'll use in
most scripts; bigger apps tend to pass dedicated service-module
paths instead.

Per-child knobs
---------------
Both spawn methods accept per-child config so one weird child
doesn't have to drag the whole tree along:

- ``loglevel='cancel'`` — crank console logging for just this
  subactor (the ``TRACTOR_LOGLEVEL`` env var overrides whatever
  the *root* was passed, handy for test runs).
- ``debug_mode=True`` — arm the crash-handling REPL machinery
  for just this child instead of tree-wide, i.e. the selective
  flavor of ``open_nursery(debug_mode=True)``; see
  :doc:`/guide/debugging` for the multi-process debugger tour.
- ``infect_asyncio=True`` — run the child with ``trio`` as an
  ``asyncio`` guest, aka "infected asyncio" mode.
- ``enable_transports=['uds']`` — pick the IPC transport this
  child should listen on (default ``'tcp'``).

Trees all the way down
----------------------
Since any actor can open an ``ActorNursery``, supervision trees
compose to arbitrary depth: a subactor can be a supervisor of
*its own* subactors, with every level holding the same SC
guarantees — error relay up, cancellation down, no orphans.

.. literalinclude:: ../../examples/nested_actor_tree.py
   :caption: examples/nested_actor_tree.py
   :language: python

Here the root spawns a ``supervisor`` actor whose RPC task opens
its *own* nursery and spawns the leaf workers; one call from the
root fans out through the middle layer and the aggregate comes
back up. Teardown ripples in reverse: the leaves are reaped when
the supervisor's nursery exits, the supervisor when the root
cancels it.

Watching your tree grow
-----------------------
Actors are real processes, so your favorite system tools just
work. The house incantation runs any example beside a live
process-tree monitor::

    $TERM -e watch -n 0.1 "pstree -a $$" \
        & python examples/nested_actor_tree.py \
        && kill $!

Every subactor also sets its OS process title to a stable
``_subactor[<name>@<uuid-prefix>]`` marker, so ``htop``,
``ps`` and friends show *which actor is which* at a glance::

    pgrep -af '_subactor\['

.. seealso::

   - :doc:`/guide/rpc` — actually invoking functions through
     all these portals you've been collecting.
   - :doc:`/guide/context` — the structured, streaming-capable
     inter-actor task API.
   - :doc:`/guide/cancellation` — cross-actor cancellation and
     error propagation semantics in depth.

.. _trio: https://github.com/python-trio/trio
.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _trio-parallel: https://github.com/richardsheridan/trio-parallel
