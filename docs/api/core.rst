Runtime and spawning
====================

The core lifecycle API: boot the runtime in your root process,
spawn trio-"actors" (processes running ``trio.run()`` task trees)
under a one-cancels-all supervisor, and talk to them through
portals. This is structured concurrency (SC) applied *transitively*:
every spawned process is owned by a nursery block and errors
`always propagate`_. If you can create zombies it **is a bug**.

.. currentmodule:: tractor

Booting the runtime
-------------------

.. autofunction:: open_root_actor

.. note::

   The env vars ``TRACTOR_LOGLEVEL`` and ``TRACTOR_SPAWN_METHOD``
   override the ``loglevel`` / ``start_method`` params so you can
   crank verbosity or swap spawn backends without touching app
   code. Exactly **one** IPC transport may be enabled per actor
   (see ``enable_transports`` and :doc:`/api/ipc`).

.. autofunction:: run_daemon

Spawning actors
---------------

.. d2:: diagrams/actor_tree.d2
   :caption: A supervised actor (process) tree.
   :margin:
   :alt: root actor supervising a tree of subactors

.. autofunction:: open_nursery

.. autoclass:: ActorNursery
   :members: start_actor,
             run_in_actor,
             cancel,
             cancel_called,
             cancelled_caught

.. note::

   :meth:`ActorNursery.start_actor` (daemon actor + portal) is the
   blessed spawning primitive; pair it with
   ``Portal.open_context()`` for SC-linked remote tasks.
   :meth:`ActorNursery.run_in_actor` is a *convenience* one-shot —
   spawn, run a single task, auto-cancel after the result — slated
   to be rebuilt as a high-level wrapper, so don't design around
   it as the core model.

.. deprecated:: 0.1.0a6

   ``ActorNursery.cancelled`` warns; use
   :attr:`ActorNursery.cancel_called` and
   :attr:`ActorNursery.cancelled_caught`. The ``rpc_module_paths``
   kwarg is likewise deprecated in favor of ``enable_modules``.

Portals
-------

A :class:`Portal` "opens a portal" into a peer actor's memory
domain: you call functions and start SC-linked tasks *over IPC* as
though they were local, with results, errors and cancellation
flowing back `exactly like trio`_.

.. autoclass:: Portal
   :members: run,
             run_from_ns,
             open_stream_from,
             wait_for_result,
             cancel_actor,
             chan

.. deprecated:: 0.1.0a6

   ``Portal.result()`` warns; use :meth:`Portal.wait_for_result`.
   The str-form ``Portal.run('mod.path', 'fn_name')`` also warns;
   pass a function *object* whose module is listed in the target's
   ``enable_modules``. ``Portal.channel`` is the legacy spelling
   of :attr:`Portal.chan`.

.. autofunction:: tractor._context.open_context_from_portal

.. note::

   :func:`~tractor._context.open_context_from_portal` is bound as
   the **method-alias** ``Portal.open_context()`` — that's the
   spelling you should actually call:
   ``portal.open_context(fn, **kwargs)``. See :doc:`/api/context`
   for the full ``Context`` + ``MsgStream`` API it unlocks.

.. note::

   :meth:`Portal.cancel_actor` cancels the *whole* remote runtime
   and process (machine-level), not a single task — use
   :meth:`Context.cancel` for task-level cancellation. Pass
   ``raise_on_timeout=True`` to get an ``ActorTooSlowError`` you
   can escalate per SC discipline (see :doc:`/api/errors`).

Clusters
--------

.. autofunction:: open_actor_cluster

Spawn a *flat* cluster of ``count`` worker actors (default: one
per core) all serving the RPC ``modules`` list, yielding a
``dict[str, Portal]`` keyed by actor name. Handy for
embarrassingly parallel fan-out; see ``examples/quick_cluster.py``.

Runtime introspection
---------------------

.. autofunction:: current_actor

.. autoclass:: Actor
   :members: aid,
             name,
             uid,
             is_registrar,
             is_infected_aio,
             cancel_soon

.. note::

   :class:`Actor` is the per-process runtime singleton (msg loop,
   RPC scheduling, IPC server) — you never instantiate it yourself
   and should normally only touch the identity/introspection
   surface listed above. The canonical identity type is
   :attr:`Actor.aid` (a ``tractor.msg.Aid`` struct);
   :attr:`Actor.uid` is the legacy ``(name, uuid)`` 2-tuple which
   is still pervasive in logs and error metadata.

.. deprecated:: 0.1.0a6

   ``Actor.is_arbiter`` warns; use :attr:`Actor.is_registrar`.
   The ``arbiter_addr`` constructor kwarg is deprecated for
   ``registry_addrs``.

.. autofunction:: current_ipc_ctx

.. autofunction:: is_root_process

.. autofunction:: get_runtime_vars

.. seealso::

   :doc:`/api/context` for the SC-linked remote task API,
   :doc:`/api/discovery` for finding actors by name, and the
   guided tours in :doc:`/guide/spawning`, :doc:`/guide/rpc` and
   :doc:`/guide/context`.

.. _always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
.. _exactly like trio: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-semantics
