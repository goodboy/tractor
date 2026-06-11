Trio patterns: ``tractor.trionics``
===================================

Sugary structured concurrency (SC) patterns for plain :mod:`trio`
code — **no actor runtime required**. These helpers grew out of
real distributed-system needs in ``tractor`` apps but every one of
them works in a single-process program too; import via
``from tractor import trionics``.

.. currentmodule:: tractor.trionics

Context-manager helpers
-----------------------

.. autofunction:: gather_contexts

.. autofunction:: maybe_open_context

.. autofunction:: maybe_open_nursery

.. note::

   :func:`gather_contexts` is "a nursery for async context
   managers": it enters N acms concurrently and yields their
   values in input order. :func:`maybe_open_context` is the
   actor-wide cache/multiplex layer on top — the first task pays
   the acm setup cost, later callers get ``(cache_hit=True, ...)``
   and share the same value until all users exit.

Broadcast fan-out
-----------------

.. autofunction:: broadcast_receiver

.. autoclass:: BroadcastReceiver
   :members: receive,
             subscribe,
             aclose

.. autoexception:: Lagged
   :show-inheritance:

A single-producer, many-consumer broadcast layer over any
``trio``-style receive channel: non-lossy for the *fastest*
consumer while slower consumers raise :class:`Lagged` (a
:class:`trio.TooSlowError` subtype) once they fall behind the
internal ring. This is exactly the machinery behind
:meth:`tractor.MsgStream.subscribe` — see
``examples/streaming_broadcast_fanout.py``.

ExceptionGroup helpers
----------------------

.. autofunction:: collapse_eg

.. autofunction:: maybe_raise_from_masking_exc

.. note::

   :func:`collapse_eg` "un-nests" single-exception
   :class:`ExceptionGroup` wrappers from strict-eg ``trio``
   nurseries so your ``except`` clauses match the original error;
   :func:`maybe_raise_from_masking_exc` surfaces real errors that
   would otherwise be masked by :class:`trio.Cancelled` during
   teardown.

.. seealso::

   :doc:`/api/context` for the IPC-stream consumer of
   :class:`BroadcastReceiver`, :doc:`/guide/streaming` for
   fan-out in a worked pipeline, and the `trio docs`_ for the
   underlying channel and `nursery`_ semantics these helpers
   compose.

.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
