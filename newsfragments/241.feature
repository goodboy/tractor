Introduce a new `sub-package`_ that exposes all our high(er) level trio primitives and goodies, most importantly:

- A new ``open_actor_cluster`` procedure is available for concurrently spawning a number of actors.
- A new ``gather_contexts`` procedure is available for concurrently entering a sequence of async context managers.

.. _sub-package: ../tractor/trionics
