Quickstart
==========
Time to spawn something B)

If you take one thing from this page make it this: ``tractor``
**is just** ``trio`` - but with nurseries for process management
and cancel-able streaming IPC. Every "*actor*" you'll meet below
is a plain Python **process** running its own ``trio.run()``
scheduled task tree, linked back to its parent through an IPC
protocol which keeps the whole tree `structured concurrency`_
(SC) compliant end-to-end. If you know your nursery_ semantics
you already know most of ``tractor``; we just stretch them across
the process boundary.

.. d2:: diagrams/actor_tree.d2
   :alt: a supervision tree of actor processes
   :margin:
   :caption: every arrow is a parent which **must wait** on its kids

Your first actor tree
---------------------
``trio`` takes the hard-line position that a parent task **must
wait** on the children it spawns; causality_ is paramount! So
does ``tractor``, one abstraction layer up:
``tractor.open_nursery()`` yields an ``ActorNursery`` which
**must** wait on its spawned *subactors* to complete (or error)
before the ``async with`` block exits, in the same causal_ way a
``trio`` nursery waits on its subtasks. That includes any one
child's crash cancelling all of its siblings: *one-cancels-all*
supervision, `exactly like trio`_.

Enough preamble, spawn a process:

.. literalinclude:: ../../examples/actor_spawning_and_causality.py
   :caption: examples/actor_spawning_and_causality.py
   :language: python

Run it::

    $ python examples/actor_spawning_and_causality.py
    Dang that's beautiful

What's going on here?

- ``trio.run(main)`` starts the **root actor**; the ``tractor``
  runtime boots *implicitly* inside ``tractor.open_nursery()``
  whenever it isn't already up. No special entrypoint, no
  framework takeover - it's just a ``trio`` app,
- inside ``main()`` a *subactor* is spawned via
  ``ActorNursery.run_in_actor()`` and told to run exactly one
  function: ``cellar_door()``,
- you get back a ``Portal``: your handle for invoking tasks in
  the new process's (separate!) memory domain. We lean on it
  much harder in the next section,
- the subactor, *some_linguist*, boots a fresh ``trio.run()`` in
  a **new process** and executes ``cellar_door()`` as its *main
  task* (note the child proving it is *not* the root with
  ``tractor.is_root_process()``), then ships the return value
  back over IPC,
- the parent grabs that *final result* with
  ``await portal.wait_for_result()``, much like you'd expect
  from a "future" - except causality is preserved: the nursery
  block only exits once the child is *done*, dead, and reaped.

.. margin:: Just need a worker pool?

   If all you want is to throw *sync* functions at your cores,
   also check out trio-parallel_. ``tractor`` is aimed at
   structured, (possibly) distributed *trees* of cooperating
   ``trio`` programs; a worker pool is a trivial special case.

.. note::

   ``run_in_actor()`` is the *convenience* wrapper: one-shot
   spawn-run-reap semantics for when a subactor's entire job is
   a single function call. The core primitives are
   ``ActorNursery.start_actor()`` (next up) paired with
   ``Portal.open_context()`` for full, SC-linked cross-actor
   dialogs - see :doc:`/guide/context`.

Daemon actors and RPC
---------------------
A ``run_in_actor()``-spawned actor terminates when its main task
returns. But often you want long-lived *daemon* actors instead:
spawned once, then serving (allowlisted) RPC requests until told
otherwise. That's ``start_actor()``:

.. literalinclude:: ../../examples/actor_spawning_and_causality_with_daemon.py
   :caption: examples/actor_spawning_and_causality_with_daemon.py
   :language: python

Two lifetime rules to internalize:

- a ``run_in_actor()`` actor lives exactly as long as its main
  task; the nursery waits for that function (and thus the
  process) to complete before unblocking,
- a ``start_actor()`` actor *lives forever* - an RPC daemon the
  nursery will happily wait on **indefinitely** - until some
  task explicitly cancels it via ``Portal.cancel_actor()`` (as
  above), or its parent nursery is cancelled wholesale.

.. tip::

   Want your *entire program* to just be a long-lived RPC
   daemon? ``tractor.run_daemon()`` is the blocking shorthand:
   it ``trio.run()``\s a root actor which serves requests until
   cancelled.

The ``enable_modules=[__name__]`` kwarg is the other thing to
notice: it lists the module paths the subactor will load and
*expose* for remote invocation.
``await portal.run(movie_theatre_question)`` works because this
very module is in that allowlist (and note we call it twice; the
daemon happily serves repeat requests). Ask for a function from
any module *not* enabled and you're denied with a
``ModuleNotExposed`` error: a simple, capability-style
restriction mechanism built on Python's own module system.

We are *processes*
------------------
Why processes (and not, say, threads)? Python has a GIL and an
`actor model`_ by definition shares **nothing** between its
concurrent units, so real OS processes are the natural fit: you
get all your cores locally, and since actors only ever talk via
IPC, the exact same code distributes over multiple hosts without
modification.

Of course, the moment you hear "process trees" you should be
asking: *what about zombies?* Watch ``tractor`` eat one for
breakfast - run this while monitoring your process tree::

    $TERM -e watch -n 0.1  "pstree -a $$" \
        & python examples/parallelism/we_are_processes.py \
        && kill $!

.. literalinclude:: ../../examples/parallelism/we_are_processes.py
   :caption: examples/parallelism/we_are_processes.py
   :language: python

.. margin:: Who's who in ``pstree``?

   Every subactor (best-effort, via the optional
   ``setproctitle`` dep) re-titles its OS process like
   ``_subactor[worker_0@<pid>]``, so ``pstree``/``htop``/
   ``pgrep -f`` can tell your actors apart at a glance.

You'll see something like (one subactor per core - 24 on this box,
trimmed here)::

    $ python examples/parallelism/we_are_processes.py
    This tree will self-destruct in 2s..

    Started ep-task in subactor,
    0::'worker_0'@218140

    Started ep-task in subactor,
    2::'worker_2'@218134

    Started ep-task in subactor,
    1::'worker_1'@218137

    Started ep-task in subactor,
    3::'worker_3'@218132

    Zombies Contained

(The ``Started ep-task`` lines land in whatever order the OS
schedules them; they're separate *processes*, racing, and that's
the point.)

One subactor is spawned per core - concurrently, from background
``trio`` tasks, so each child's cold ``import tractor`` overlaps
instead of stacking. Each runs a ``@tractor.context``
``endpoint()`` that ``ctx.started()``-hands its name and pid back
through ``Portal.open_context()`` (those ``Started ep-task``
lines), then parks in ``trio.sleep_forever()``. Then the root
*crashes on purpose* and the ``ActorNursery`` responds with hard
``trio`` discipline: every child is cancelled, every process is
reaped, the error propagates to ``trio.run()``, and your terminal
prints ``Zombies Contained``. No orphans, no ``kill -9``
archaeology in ``htop`` afterwards.

.. note::

   **The zombie-safety guarantee**: ``tractor`` tries to protect
   you from zombies, *no matter what*. If you can create zombie
   child processes (without using a system signal) it **is a
   bug** - please report it so we can hunt it down.

A trynamic first scene
----------------------
So far the root actor has done all the talking, but subactors
can just as well discover and call *each other*. Let's direct a
couple actors and have them run their lines for the hip new film
we're shooting:

.. literalinclude:: ../../examples/a_trynamic_first_scene.py
   :caption: examples/a_trynamic_first_scene.py
   :language: python

The script of the scene (runtime ``INFO`` log lines trimmed)::

    $ python examples/a_trynamic_first_scene.py
    Alright... Action!
    Hi my name is gretchen
    Hi my name is donny
    CUTTTT CUUTT CUT!!! Donny!! You're supposed to say...

The new tricks in play:

- two subactors, *donny* and *gretchen*, are each told to run
  ``say_hello()`` targeting the *other* by name,
- ``tractor.wait_for_actor()`` blocks until the named peer has
  registered with the tree's *registrar* (every actor announces
  itself at boot), then yields a ``Portal`` connected
  **directly** to that peer,
- each actor invokes its partner's ``hi()`` over that portal:
  actor-to-actor RPC with the root merely *directing* - and both
  final lines flow back to ``main()`` via
  ``await portal.wait_for_result()``,
- ``tractor.log.get_console_log("INFO")`` cranks up runtime
  logging so you can watch the spawn/register/cancel machinery
  narrate itself; remove it for a quiet set.

Cross-actor calls look just like (async) function calls; there
are no proxy objects and no shared references, only messages B)

Crash handling, native feeling
------------------------------
One last teaser before the guide proper. Flip exactly one
switch:

.. code:: python

    async with tractor.open_nursery(
        debug_mode=True,
    ) as an:
        ...

and any crash, in *any* actor at *any* depth of the tree, drops
your terminal into a multi-process-safe pdbp_ REPL at the
offending frame, with the rest of the tree held back from
clobbering the tty. ``await tractor.pause()`` likewise gives you
a breakpoint that *just works* inside subprocesses. We think it
might be the first native multi-process debugging UX for Python;
get the full tour in :doc:`/guide/debugging`.

Where to next?
--------------
You can now boot a runtime, spawn one-shot and daemon actors,
make cross-process RPC calls, and contain zombies: that's the
on-ramp done. The guide takes each subsystem deeper,

- :doc:`/explain/sc-distributed` - the structured concurrency
  worldview and how ``tractor`` extends it across processes,
- :doc:`/guide/spawning` - everything ``ActorNursery``: spawn
  kwargs, lifetimes and supervision semantics,
- :doc:`/guide/rpc` - the ``Portal`` in depth: calling into
  another actor's memory domain,
- :doc:`/guide/context` - the core API: ``@tractor.context``
  endpoints, the ``ctx.started()`` handshake, and SC-linked
  cross-actor task pairs,
- :doc:`/guide/streaming` - bidirectional ``MsgStream`` dialogs
  and fan-out broadcasting,
- :doc:`/guide/debugging` - the multi-process REPL, crash
  handling mode, and ``tractor.pause()``,
- :doc:`/guide/asyncio` - "infected ``asyncio``" mode: SC
  supervision wrapped around ``asyncio`` tasks,
- :doc:`/guide/discovery` - registries, service daemons, and
  finding actors from anywhere in (or out of) the tree.

.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _causal: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#causality
.. _exactly like trio: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-semantics
.. _actor model: https://en.wikipedia.org/wiki/Actor_model
.. _trio-parallel: https://github.com/richardsheridan/trio-parallel
.. _pdbp: https://github.com/mdmintz/pdbp
