Debugging and devx: ``tractor.devx``
====================================

Multi-process debugging that actually works: boot the tree with
``open_root_actor(debug_mode=True)`` (or pass it to
``open_nursery()``) and any crash or explicit pause in *any* actor
acquires a tree-global TTY lock and drops you into a
`pdbp`_-powered REPL — one actor at a time, ``SIGINT`` shielded,
no garbled terminals. The top-level helpers below are the daily
drivers; the rest of the toolbox lives under ``tractor.devx``.

.. currentmodule:: tractor

Pausing and post-mortems
------------------------

.. autofunction:: pause

.. autofunction:: pause_from_sync

.. note::

   :func:`pause_from_sync` needs the `greenback`_ portal: boot
   with ``open_root_actor(maybe_enable_greenback=True)`` (mind
   the `performance implications`_). With ``debug_mode`` on, the
   built-in ``breakpoint()`` is also remapped to a
   ``tractor``-safe equivalent.

.. autofunction:: post_mortem

.. deprecated:: 0.1.0a6

   ``tractor.breakpoint()`` warns and simply calls :func:`pause`
   — use :func:`tractor.pause` (async) or
   :func:`tractor.pause_from_sync` in new code.

Crash handling for CLIs and sync entrypoints
--------------------------------------------

.. currentmodule:: tractor.devx

.. autofunction:: open_crash_handler

.. autofunction:: maybe_open_crash_handler

.. note::

   Both are *sync* context managers usable before (or without)
   ``trio.run()`` — wrap your CLI ``main()`` to get a post-mortem
   REPL on any uncaught exception instead of a bare traceback.

Runtime hang-hunting
--------------------

.. autofunction:: enable_stack_on_sig

With stackscope_ integration enabled (also via
``open_root_actor(enable_stack_on_sig=True)`` or the
``TRACTOR_ENABLE_STACKSCOPE`` env var) a ``SIGUSR1`` triggers a
full trio task-tree dump from every actor — works on live,
*non*-debug-mode trees too:

.. code:: sh

   pkill --signal SIGUSR1 -f <part-of-your-cmd>

Dumps also tee to ``/tmp/tractor-stackscope-<pid>.log`` so you
still get output under captured/CI stdio.

Lower-level debug plumbing
--------------------------

.. autofunction:: mk_pdb

.. autofunction:: maybe_wait_for_debugger

:func:`maybe_wait_for_debugger` is mainly useful in runtime/test
code that must avoid tearing down a tree while a child still
holds the global debug lock.

.. seealso::

   :doc:`/api/errors` for the boxed error types you'll inspect
   from the REPL, :doc:`/api/core` for the ``debug_mode`` /
   ``maybe_enable_greenback`` / ``enable_stack_on_sig`` boot
   flags on :func:`tractor.open_root_actor`, and
   :doc:`/guide/debugging` for the guided multi-actor REPL tour.

.. _pdbp: https://github.com/mdmintz/pdbp
.. _greenback: https://greenback.readthedocs.io/en/latest/
.. _performance implications: https://greenback.readthedocs.io/en/latest/principle.html#performance
.. _stackscope: https://github.com/oremanj/stackscope
