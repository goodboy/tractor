Testing tips
============

``tractor``'s test suite is a different kind of beast than your
average single-proc pytest run: nearly every test spawns a real
**process tree**, hammers on cancellation under structured
concurrency (SC), and tears the whole thing down again — hundreds
of times per session. This page collects the tips, knobs and
one-liners that make hacking on (and with) the suite pleasant.

Running the suite
-----------------

This is a uv_-managed project, so after cloning it's just::

    uv sync --dev
    uv run pytest tests/

Expect a *lot* of process churn; the suite is effectively a
rolling chaos exercise for the runtime.

The classic fix-iterate loop when something breaks::

    # stop at the first failure
    uv run pytest tests/ -x

    # then iterate on just the failures til green
    uv run pytest --lf -x

``--lf`` (last-failed) re-runs only what failed previously, so
combined with ``-x`` you get a tight one-test-at-a-time repair
loop.

Suite-specific flags
********************

The repo auto-loads the bundled ``tractor._testing.pytest`` plugin
(via ``addopts`` in ``pyproject.toml``) which adds a few extra
flags:

- ``--spawn-backend <key>``: pick the process spawn backend for
  the session (default ``'trio'``); same keys as the
  ``start_method`` runtime argument,

- ``--tpt-proto <key> [...]``: which IPC transport(s) opting-in
  suites should run against, eg. ``--tpt-proto uds``,

- ``--tpdb`` / ``--debug-mode``: flip on the ``debug_mode``
  fixture so debugger-aware tests boot their trees with the
  crash-REPL enabled,

- ``--enable-stackscope``: install the ``SIGUSR1`` task-tree dump
  handler in pytest *and* every spawned subactor — much lighter
  than a full debug-mode run when you only need stack visibility
  during a hang hunt,

- ``--ll <level>`` / ``--tl <level-or-spec>``: console loglevels;
  ``--tl`` targets the ``tractor``-as-runtime logger and accepts
  a per-subsystem spec like ``'devx:runtime,trionics:cancel'``.

Watch the tree grow
-------------------

The single most useful trick while the suite (or any ``tractor``
app) runs: keep a live ``pstree`` view going in a side terminal::

    watch -n 0.1 "pstree -a $(pgrep -f pytest)"

You'll see actor processes pop in and out of existence as each
test builds and reaps its tree. Launch it *after* pytest is up
(the pid is substituted once, at ``watch`` startup).

Every subactor also sets its OS process title (via
``setproctitle``) to ``_subactor[<name>@<uuid-prefix>]`` so the
tree view shows *which actor is which* at a glance — and targeted
greps stay easy::

    pgrep -af '_subactor\['

For a single example script, the repo's signature incantation
spawns the watcher alongside your program and cleans it up after::

    $TERM -e watch -n 0.1 "pstree -a $$" \
        & python examples/parallelism/single_func.py \
        && kill $!

Env-var knobs
-------------

Two env-vars override their corresponding runtime arguments
*globally* — no application (or test) code changes required:

``TRACTOR_SPAWN_METHOD``
    Wins over any caller-passed ``start_method`` so you can drive
    the whole suite (or any app) under a different spawn backend::

        TRACTOR_SPAWN_METHOD=mp_spawn uv run pytest tests/ -x

``TRACTOR_LOGLEVEL``
    Wins over any caller-passed ``loglevel``; crank (or silence)
    runtime console verbosity wholesale::

        TRACTOR_LOGLEVEL=cancel uv run pytest tests/ -x -s

``TRACTOR_ENABLE_STACKSCOPE``
    Force-install the ``SIGUSR1`` task-tree dump handler in every
    actor, debug-mode or not; then
    ``pkill --signal SIGUSR1 -f <part-of-cmd>`` dumps every
    actor's live ``trio`` task tree.

Debug mode vs. pytest capture
-----------------------------

The tree-wide crash-to-REPL experience (``debug_mode=True`` plus
``await tractor.pause()``) requires a **real tty**, and pytest's
default output capturing swallows exactly that. When you want to
interact with the REPL from inside a test run, disable capture::

    uv run pytest tests/test_foo.py -x -s

(``-s`` is shorthand for ``--capture=no``.)

Tests should request the ``debug_mode`` fixture (driven by the
``--tpdb`` flag) rather than hard-coding it, so that normal CI
runs stay non-interactive.

For *automated* REPL interaction — asserting on prompt output,
sending debugger commands — you can't just turn capture off;
instead do what ``tests/devx/`` does: drive a child Python program
through pexpect_ on a real pseudo-tty and pattern-match the
``(Pdb+)`` prompts. See ``tests/devx/test_debugger.py`` for many
worked patterns.

Examples *are* tests
--------------------

Every script under ``examples/`` is run as a subprocess by
``tests/test_docs_examples.py``; since these docs
``literalinclude`` those same scripts, the code you read here is
CI-verified on every push and can never silently rot B)

Conventions when adding a new example:

- make it a standalone runnable script with the usual guard::

      if __name__ == '__main__':
          trio.run(main)

- it must exit cleanly (returncode ``0``) within the per-example
  timeout (~16s locally, with headroom auto-added in CI and under
  cpu-freq scaling) — keep sleeps short,

- any stderr line containing ``Error`` fails the test, so silence
  or assert-around expected error output,

- don't crank ``tractor`` logging inside an example: subprocess
  pipe **backpressure can deadlock** the run (ask us how we
  know..),

- filenames starting with ``_`` are skipped (the WIP convention),
  as are the special subdirs (``debugging/``, ``integration/``,
  ``advanced_faults/``, ``trio/``) which are driven by their own
  dedicated suites instead.

Drop your script in, run the example suite, profit::

    uv run pytest tests/test_docs_examples.py -x

Zombie cleanup
--------------

First, the contract: ``tractor`` **always** reaps its children —
if you can create a zombie process (without resorting to
untrappable signals) it **is a bug**, please report it!

That said, while hacking on the *runtime itself* you can
definitely wedge things — a ``SIGKILL``-ed pytest, a half-broken
spawn backend — and strand subactor procs plus their shm segments
and UDS socket files. The repo ships a dedicated cleanup tool::

    uv run scripts/tractor-reap --shm --uds

It's SC-polite even as a reaper: matched processes get ``SIGINT``
first with a bounded grace window — so actor runtimes can run
their ``trio`` teardown paths — escalating to ``SIGKILL`` only as
a last resort. The ``--shm`` sweep unlinks ``/dev/shm/`` segments
that no live process has open (it leans on psutil_, already in
your dev venv, to check live mappings and fds) and ``--uds``
clears socket files whose binder pid is dead.

Testing your own ``tractor`` app
--------------------------------

The same plugin the suite uses ships in the package, so your
project can load it too::

    [tool.pytest.ini_options]
    addopts = ['-p tractor._testing.pytest']

That buys you the CLI flags above plus a set of fixtures —
``loglevel``, ``debug_mode``, ``reg_addr`` (a session-unique
registrar address so concurrent runs and other live ``tractor``
apps on the host can't cross-talk) — and the ``@tractor_test``
decorator:

.. code:: python

    import tractor
    from tractor._testing import tractor_test

    @tractor_test
    async def test_my_service(
        reg_addr: tuple,
        loglevel: str,
    ):
        # already inside a root actor's trio task!
        async with tractor.open_nursery() as an:
            ...

The decorator boots a root actor around your (async) test fn,
wires any of the special fixtures you declare (``reg_addr``,
``loglevel``, ``start_method``, ``debug_mode``) into
``open_root_actor()``, and runs the body as the root-most task
under a wall-clock ``trio.fail_after()`` guard.

General advice that has served this suite well:

- bound waits with ``trio.fail_after()`` *inside* tests; global
  pytest timeout plugins interact badly with multi-process
  ``trio`` teardown,

- use the ``reg_addr`` fixture (or otherwise randomize your
  registry addrs) so leftover registrars from prior runs can't
  contaminate lookups,

- assert on **structured outcomes** — eg.
  ``RemoteActorError.boxed_type`` or
  ``ContextCancelled.canceller`` — not on log text.

.. note::
   ``tractor._testing`` is still an underscore-internal namespace:
   shipped and handy, but its API may shift between alpha
   releases.

(This page exists thanks to the ask in `#126`_.)

.. seealso::
   - :doc:`/guide/discovery` — how registrar wiring (the thing
     ``reg_addr`` randomizes) works in the runtime proper.
   - :doc:`/project/dev-tips` — contributor-oriented extras:
     releases, log-system tracing, tree-monitoring recipes.

.. _uv: https://docs.astral.sh/uv/
.. _pexpect: https://pexpect.readthedocs.io/en/stable/
.. _psutil: https://psutil.readthedocs.io/en/latest/
.. _#126: https://github.com/goodboy/tractor/issues/126
