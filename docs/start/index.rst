Getting started
===============
Welcome aboard B)

Real talk before any code: the first step to grok ``tractor`` is
to get an intermediate knowledge of ``trio`` and **structured
concurrency** (SC). ``tractor`` **is just** ``trio`` - but with
nurseries for process management and cancel-able streaming IPC -
so every rule you already know about task lifetimes, cancellation
and error propagation keeps holding, just now across process (and
host!) boundaries. Some great places to start are,

- the seminal `blog post`_,
- obviously the `trio docs`_,
- wikipedia's nascent SC_ page,
- the fancy diagrams @ libdill-docs_.

Once you've taken in (some of) the canon, get installed and go
spawn your first actor tree:

.. toctree::
   :maxdepth: 1

   install
   quickstart

.. seealso::

   Already installed and itching? Jump straight to
   :doc:`/start/quickstart`; once you're through the on-ramp the
   :doc:`guide pages </guide/index>` take each subsystem deeper.

.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _SC: https://en.wikipedia.org/wiki/Structured_concurrency
.. _libdill-docs: https://sustrik.github.io/libdill/structured-concurrency.html
