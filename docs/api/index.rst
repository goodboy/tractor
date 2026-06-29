API reference
=============

This is the curated reference for ``tractor``'s public surface: the
names you can import and lean on without reading runtime internals.
Everything below is re-exported at the top level (``import
tractor``) unless a page says otherwise; subsystems like
``tractor.msg``, ``tractor.trionics``, ``tractor.to_asyncio``,
``tractor.devx`` and ``tractor.log`` are importable as submodules.

``tractor`` is "just trio_" extended across processes: every API
here is designed to keep the structured concurrency (SC) rules you
already know from the `trio docs`_ intact across the process
boundary. If a name isn't documented here it's an internal — expect
it to change without notice B).

.. currentmodule:: tractor

Most-used names at a glance:

.. autosummary::
   :nosignatures:

   open_root_actor
   open_nursery
   run_daemon
   ActorNursery
   Portal
   context
   Context
   MsgStream
   open_actor_cluster
   find_actor
   wait_for_actor
   get_registry
   current_actor
   current_ipc_ctx
   is_root_process
   get_runtime_vars
   RemoteActorError
   ContextCancelled
   MsgTypeError
   pause
   post_mortem
   Channel

.. toctree::
   :maxdepth: 1
   :caption: Reference pages

   core
   context
   discovery
   errors
   msg
   trionics
   to_asyncio
   devx
   ipc

Where to next? If you're new, start with the runtime and spawning
APIs in :doc:`/api/core`, then graduate to the inter-actor task
linking model in :doc:`/api/context` — it's the heart of the whole
system.

.. _trio: https://github.com/python-trio/trio
.. _trio docs: https://trio.readthedocs.io/en/latest/
