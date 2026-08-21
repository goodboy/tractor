RPC: calling into other actors
==============================
Every spawn call from :doc:`/guide/spawning` hands you back
a :class:`~tractor.Portal`: a live handle for calling into
another actor's **memory domain**. The name is borrowed from
``trio``'s portal concept — an object you use to submit work
*into* a separate concurrency domain — except here that domain
is a whole other process.

.. d2:: diagrams/runtime_stack.d2
   :margin:
   :caption: The layers a ``portal.run()`` request rides through.
   :alt: app, tractor runtime, IPC channel and OS process layers

There are **no proxy objects** and no special calling
conventions: you pass a plain function reference plus keyword
args, and Python's normal ``await``-able semantics apply. The
function just happens to *run somewhere else*; from the calling
task it looks as though it was called locally. And since this is
all structured concurrency (SC) under the hood, the remote task
runs inside the callee's supervised task tree while its result
— or its failure, as a boxed
:exc:`~tractor.RemoteActorError` — always comes back to *you*.

``Portal.run()``: pass the function, not a string
-------------------------------------------------
:meth:`~tractor.Portal.run` schedules an async function as
a **new task** in the remote actor and waits on its result:

.. code:: python

    async with tractor.open_nursery() as an:
        portal = await an.start_actor(
            'service',
            enable_modules=[__name__],
        )
        answer = await portal.run(movie_theatre_question)

The rules of engagement:

- the target must be an **async function** and its defining
  module must be in the callee's ``enable_modules`` allowlist,
  else an :exc:`~tractor.ModuleNotExposed` error is relayed
  back (see :doc:`/guide/spawning` for the capability-allowlist
  story).
- arguments are passed **by keyword only**; they ride the IPC
  layer as msgspec_-encoded msgs, so keep them serializable.
- every call schedules a *fresh* task remotely — call it twice
  and the callee runs two tasks, each supervised in its own
  right.
- remote exceptions re-raise locally as
  :exc:`~tractor.RemoteActorError` with the original type
  preserved via ``.boxed_type``.

.. note::

   Passing dotted-path *strings* to ``run()`` is an ancient,
   deprecated form; always pass the function reference. If you
   really need name-based addressing use ``run_from_ns()``
   below.

Namespaced daemons: ``run_from_ns()``
-------------------------------------
Sometimes the calling process can't (or shouldn't) import the
target function — think a long-running rpc-daemon serving
modules your client never loads. For that,
:meth:`~tractor.Portal.run_from_ns` takes the explicit
namespace path:

.. code:: python

    await portal.run_from_ns('mypkg.service', 'ping')

This is literally how ``.run()`` works underneath: the pair is
encoded as a ``'mod.path:func'`` style msg and resolved against
the callee's enabled modules.

One special namespace exists: ``'self'`` resolves to the remote
:class:`~tractor.Actor` instance, i.e. the runtime itself. It's
how internal machinery (cancel requests, registry ops) travels;
don't build your app on it.

One-shot subactors: ``to_actor.run()``
--------------------------------------
When the call should own a fresh subactor whose entire job is one
function call, :func:`tractor.to_actor.run` spawns it, runs the task,
returns its result and reaps the process — all in one blocking call:

.. code:: python

    from functools import partial

    final = await tractor.to_actor.run(
        partial(fib, n=10),
        an=an,
    )

Semantics worth knowing:

- it blocks until the remote task returns, re-raising any
  remote error in the usual boxed form right in the calling
  task.
- placement also determines process ownership: ``an=`` spawns and
  reaps a fresh child in an existing actor nursery, while passing
  neither does the same in a private call-scoped nursery (booting
  the runtime if needed). ``portal=`` instead runs one linked task
  in an existing actor; it neither spawns nor reaps that actor, so
  the portal's owner remains responsible for its lifetime.
- concurrency composes the plain ``trio`` way: schedule
  multiple ``run()`` calls into a local task nursery (see
  ``examples/parallelism/to_actor_one_shots.py``).

A reused actor must expose both the target module and the
``to_actor`` context trampoline:

.. code:: python

    async with tractor.open_nursery() as an:
        portal = await an.start_actor(
            'worker',
            enable_modules=[
                __name__,
                tractor.to_actor.MODULE,
            ],
        )
        try:
            final = await tractor.to_actor.run(
                partial(fib, n=10),
                portal=portal,
            )
        finally:
            await portal.cancel_actor()

Pure RPC daemons: ``run_daemon()``
----------------------------------
When a process's *only* job is to sit at the root of its own
tree and serve RPC, skip the boilerplate with
:func:`tractor.run_daemon`:

.. code:: python

    import tractor

    tractor.run_daemon(
        ['mypkg.service'],
        name='service',
    )

It's a blocking convenience (it calls ``trio.run()`` for you):
boot a root actor with the given modules enabled for RPC, then
sleep until cancelled. Pair it with the discovery system —
:func:`tractor.find_actor` / :func:`tractor.wait_for_actor`
from a *separate* program — and you've got a tiny service
architecture with zero framework ceremony; see
``examples/service_daemon_discovery.py`` for the full pattern.

Fan-out: RPC through nested trees
---------------------------------
Portals compose. An RPC task is just a ``trio`` task, so it can
open its own :class:`~tractor.ActorNursery` and portal into
*its* children — one inbound call fanning out into a whole
sub-tree of work. The mid-tier function from the nested-tree
example:

.. literalinclude:: ../../examples/nested_actor_tree.py
   :caption: examples/nested_actor_tree.py (supervisor fan-out)
   :language: python
   :pyobject: fan_out_squares

The root portals into the ``supervisor`` actor; the
supervisor's RPC task spawns the leaf workers, portals into
each, and returns the combined result back up. Failures at any
depth relay hop-by-hop as boxed errors, and cancelling the root
call tears down the entire sub-tree — SC, transitively.

When to graduate to ``Context``
-------------------------------
The :meth:`~tractor.Portal.run` method is great for one-shot,
request-response calls.
Reach for :meth:`~tractor.Portal.open_context` with an
``@tractor.context`` endpoint as soon as you want:

- a long-lived dialog with state held on both sides,
- bidirectional streaming via ``ctx.open_stream()``,
- typed payload contracts (``pld_spec``) enforced at the msg
  layer,
- or *task-scoped* cancellation: ``Context.cancel()`` cancels
  just the linked remote task, whereas
  :meth:`~tractor.Portal.cancel_actor` nukes the **entire**
  remote runtime and its process.

:func:`tractor.to_actor.run` already enters the full
:meth:`~tractor.Portal.open_context` lifecycle. The older
:meth:`~tractor.Portal.run` path instead uses the ``Context`` returned
by the lower-level ``Actor.start_remote_task()`` directly, avoiding a
``Started`` handshake but owning less lifecycle machinery. A follow-up
should factor their shared linked-task lifecycle without requiring
``Portal.run()`` to delegate through the public context API or add
another wire message. Take the full tour in
:doc:`the context guide </guide/context>`.

.. seealso::

   - :doc:`/guide/spawning` — where portals come from and how
     their actors are supervised.
   - :doc:`/guide/context` — the structured cross-actor task
     API: handshake, streaming, typed payloads.
   - :doc:`/guide/cancellation` — what happens to in-flight RPC
     when trees get torn down.

.. _msgspec: https://jcristharif.com/msgspec/
