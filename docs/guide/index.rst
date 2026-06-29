Guides
======
Task-focused walkthroughs of every major ``tractor``
subsystem, each built around real, *test-suite
verified* example scripts from the repo's
``examples/`` dir (we never copy-paste code into
docs; what you read is what CI runs).

Roughly in "first date to long term relationship"
order,

- :doc:`spawning` — actor nurseries, daemons +
  one-shot workers, process lifetimes.
- :doc:`rpc` — portals: calling into another
  process like it's a local ``await``.
- :doc:`context` — the cross-actor task-pair
  primitive at the heart of modern ``tractor``.
- :doc:`streaming` — one-way and bidirectional
  msg streams between actors.
- :doc:`cancellation` — supervision, error
  boxing + propagation, teardown discipline.
- :doc:`debugging` — the multi-process native
  REPL debugger; our flagship DX feature B)
- :doc:`discovery` — the registrar, finding
  actors by name, service patterns.
- :doc:`clustering` — quick flat process
  clusters via one ``async with``.
- :doc:`parallelism` — worker pools without
  pools; a ``concurrent.futures`` re-think.
- :doc:`asyncio` — "infected asyncio" mode:
  SC-supervise ``asyncio`` tasks from ``trio``.
- :doc:`msging` — typed IPC payloads, the wire
  msg-spec and custom codecs.
- :doc:`testing` — running + monitoring the
  test suite (and testing your own actor apps).

.. toctree::
   :hidden:
   :maxdepth: 1

   spawning
   rpc
   context
   streaming
   cancellation
   debugging
   discovery
   clustering
   parallelism
   asyncio
   msging
   testing
