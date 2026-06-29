asyncio interop: ``tractor.to_asyncio``
=======================================

"Infected asyncio" mode: spawn an actor with
``start_actor(..., infect_asyncio=True)`` and its process runs
:mod:`trio` as a `guest`_ on top of the :mod:`asyncio` loop —
letting your trio task tree drive asyncio tasks *in the same
process* while the rest of the actor tree stays pure trio. Each
trio <-> asyncio task pair is linked with structured concurrency
(SC) semantics: error or cancellation on either side tears down
both, with the cause translated cross-loop.

.. d2:: diagrams/infected_aio.d2
   :caption: A trio guest driving asyncio tasks in one actor.
   :margin:
   :alt: trio guest mode inside an asyncio-infected actor

See ``examples/infected_asyncio_echo_server.py`` for a complete
worked example.

.. currentmodule:: tractor.to_asyncio

Starting asyncio tasks from trio
--------------------------------

.. autofunction:: open_channel_from

.. autofunction:: run_task

.. note::

   :func:`open_channel_from` mirrors the
   ``Portal.open_context()`` handshake: the asyncio side calls
   ``chan.started_nowait(value)`` and that value pops out as
   ``first`` on the trio side. :func:`run_task` is the one-shot
   form — run a single asyncio-compatible coroutine fn and return
   its result to trio.

The inter-loop channel
----------------------

.. autoclass:: LinkedTaskChannel
   :members: send,
             receive,
             wait_for_result,
             subscribe,
             started_nowait,
             send_nowait,
             get,
             cancel_asyncio_task,
             closed

.. note::

   The trio side uses the async API
   (:meth:`LinkedTaskChannel.send` /
   :meth:`LinkedTaskChannel.receive`); the asyncio side uses the
   loop-safe sync/await mix
   (:meth:`LinkedTaskChannel.send_nowait` /
   :meth:`LinkedTaskChannel.get` /
   :meth:`LinkedTaskChannel.started_nowait`).

Translated exception types
--------------------------

Cross-loop failures are re-raised on the *other* side as one of
these explicit translation types, so you always know which loop
actually died first:

.. autoexception:: tractor._exceptions.AsyncioCancelled

.. autoexception:: tractor._exceptions.AsyncioTaskExited

.. autoexception:: tractor._exceptions.TrioCancelled

.. autoexception:: tractor._exceptions.TrioTaskExited

.. autoexception:: AsyncioRuntimeTranslationError
   :show-inheritance:

Guest-mode entrypoint
---------------------

``run_as_asyncio_guest()`` is the runtime-internal entrypoint that
boots trio in `guest`_ mode inside an infected actor — you get it
implicitly via ``infect_asyncio=True`` and shouldn't need to call
it yourself.

.. seealso::

   :doc:`/api/core` for the ``infect_asyncio`` spawn flag,
   :meth:`tractor.Actor.is_infected_aio` for runtime
   introspection, :doc:`/api/devx` for using the debugger from
   inside asyncio tasks, and :doc:`/guide/asyncio` for the
   guided tour.

.. _guest: https://trio.readthedocs.io/en/stable/reference-lowlevel.html?highlight=guest%20mode#using-guest-mode-to-run-trio-on-top-of-other-event-loops
