tractor
=======

.. raw:: html
   :file: _static/tractor_hero.html

``tractor`` provides parallelism via ``trio``
*"actors"*:

- independent Python **processes** each running a
  ``trio`` task tree,
- all composed into a *distributed supervision tree*
  with end-to-end SC_,
- spawning, cancellation, error propagation and
  teardown that work **across processes** (and hosts)
  exactly the way they work across tasks.

Sixty seconds of why
--------------------
.. margin:: tl;dr

   It's **just** ``trio``, but with nurseries that
   spawn *processes* and streams that cross them. If
   you can read a ``trio`` program you can read a
   ``tractor`` one — that's the whole pitch.

Spawn one actor per core, open a ``Context`` into
each — the child ``started()``-handshakes its name
and pid back — then crash the root on purpose and
watch the runtime contain the blast: errors
propagate, *every* child is reaped, zero zombies —
guaranteed (it's a bug otherwise).

.. literalinclude:: ../examples/parallelism/we_are_processes.py
   :caption: examples/parallelism/we_are_processes.py
   :language: python

Like every snippet in these docs this file lives in
the repo's ``examples/`` dir and runs under CI — docs
code that can't rot.

Dig in
------
.. grid:: 1 2 2 3
   :gutter: 3

   .. grid-item-card:: Get started
      :link: start/index
      :link-type: doc

      Install + your first actor tree in ~20 lines;
      causality, daemons and the trynamic scene.

   .. grid-item-card:: The big ideas
      :link: explain/sc-distributed
      :link-type: doc

      SC across processes, distilled — then the
      runtime architecture under it.

   .. grid-item-card:: Debug like a local
      :link: guide/debugging
      :link-type: doc

      ``await tractor.pause()`` anywhere in the
      tree: one terminal, every process, zero
      socket-juggling.

   .. grid-item-card:: Streaming + contexts
      :link: guide/context
      :link-type: doc

      Bidirectional, cancellation-safe msg streams
      between any two actors.

   .. grid-item-card:: Guides
      :link: guide/index
      :link-type: doc

      RPC, supervision, clustering, "infected
      asyncio", typed msging + more.

   .. grid-item-card:: API reference
      :link: api/index
      :link-type: doc

      The curated public surface; everything
      importable from ``tractor``.

Features
--------
- **It's just a** ``trio`` **API** — same nursery
  discipline, same cancellation semantics, one level
  up the process tree.
- *Infinitely nestable* process trees: sub-actors can
  spawn sub-actors, supervision stays transitive.
- A "native UX" **multi-process debugger REPL**:
  built on pdbp_ with tree-wide tty locking (see
  :doc:`guide/debugging`).
- Built-in, cancellation-safe **bidirectional
  streaming** via a `cheap or nasty`_ `(un)protocol`_.
- **Typed IPC**: `msgspec`_-backed wire msgs with
  optional per-dialog payload specs
  (:doc:`guide/msging`).
- Swappable process-spawn backends + modular IPC
  transports (TCP today, UDS on same-host, more
  planned).
- Optionally distributed_: the same APIs work over
  multiple hosts as on multiple cores.
- "**Infected** ``asyncio``" mode: SC-supervise
  ``asyncio`` tasks from ``trio``
  (:doc:`guide/asyncio`).
- ``trio`` extension goodies via ``tractor.trionics``
  (acm gathering, single-resource caching, broadcast
  channels).

Where do i start!?
------------------
The first step to grok ``tractor`` is to get an
intermediate knowledge of ``trio`` and **structured
concurrency** B)

Some great places to start are,

- the seminal `blog post`_,
- obviously the `trio docs`_,
- wikipedia's nascent SC_ page,
- the fancy diagrams @ libdill-docs_,

then come back and hit :doc:`start/quickstart`.

.. toctree::
   :hidden:
   :maxdepth: 2

   Get started <start/index>
   Big ideas <explain/index>
   Guides <guide/index>
   API <api/index>
   Project <project/index>

.. _trio: https://github.com/python-trio/trio
.. _SC: https://en.wikipedia.org/wiki/Structured_concurrency
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _libdill-docs: https://sustrik.github.io/libdill/structured-concurrency.html
.. _pdbp: https://github.com/mdmintz/pdbp
.. _msgspec: https://jcristharif.com/msgspec/
.. _cheap or nasty: https://zguide.zeromq.org/docs/chapter7/#The-Cheap-or-Nasty-Pattern
.. _(un)protocol: https://zguide.zeromq.org/docs/chapter7/#Unprotocols
.. _distributed: https://en.wikipedia.org/wiki/Distributed_computing
