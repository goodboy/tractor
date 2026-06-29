Install
=======
``tractor`` is still in an *alpha-near-beta-stage* for many of
its subsystems, however we are very close to having a stable
lowlevel runtime and API. Expect the occasional rough edge (and
feel free to report it!).

Supported platforms
-------------------
- **python**: ``>=3.13,<3.15`` - yes, we ride near the front of
  the release train; 3.14 additionally unlocks the experimental
  PEP-734 sub-interpreter spawn backend,
- **linux**: the primary development and CI platform,
- **macos**: officially supported,
- **windows**: currently *untested* - we disabled CI-testing on
  windows a while back and haven't had the cycles to revive it.
  It mostly worked historically; if you're a windows person we'd
  love a hand getting it back in the test matrix.

With ``uv`` (preferred)
-----------------------
We use the very hip uv_ for project mgmt and recommend you do
too. Add ``tractor`` to your project::

    uv add tractor

or, since the git ``main`` branch is often much further ahead
than any latest release (see the PyPi note below), track ``main``
directly::

    uv add "tractor @ git+https://github.com/goodboy/tractor.git"

From PyPi
---------
We ofc also offer "releases" on PyPi_::

    pip install tractor

Just note that **YMMV** since ``main`` is usually well ahead of
the latest published alpha; when in doubt go with a git install
as per above (or hack from source as per below).

From source
-----------
To run the bundled examples in-tree, or to start hacking on the
code base, clone and sync a dev env::

    git clone https://github.com/goodboy/tractor.git
    cd tractor
    uv sync --dev
    uv run python examples/rpc_bidir_streaming.py

Consider activating a virtual/project-env before starting to hack
on the code base::

    # you could use plain ol' venvs
    # https://docs.astral.sh/uv/pip/environments/
    uv venv tractor_py313 --python 3.13

    # but @goodboy prefers the more explicit (and shell agnostic)
    # https://docs.astral.sh/uv/configuration/environment/#uv_project_environment
    UV_PROJECT_ENVIRONMENT="tractor_py313"

    # hint hint, enter @goodboy's fave shell B)
    uv run --dev xonsh

.. seealso::

   All set? Go boot a process tree in :doc:`/start/quickstart`.
   And if the install fights you, swing by the `matrix channel`_
   and we'll sort it out.

.. _uv: https://docs.astral.sh/uv/
.. _PyPi: https://pypi.org/project/tractor/
.. _matrix channel: https://matrix.to/#/!tractor:matrix.org
