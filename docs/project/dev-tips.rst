Hot tips for ``tractor`` hackers
================================

This is a (perpetually WIP) guide for newcomers to the project,
mostly to do with dev, testing, CI and release gotchas, reminders
and best practises.

``tractor`` is a fairly novel project compared to most since it is
effectively a new way of doing distributed computing in Python and
is much closer to working with an "application level runtime"
(like erlang OTP or scala's akka project) than it is a traditional
Python library. As such, having an arsenal of tools and recipes
for figuring out the right way to debug problems when they do
arise is somewhat of a necessity.

Making a release
----------------

Nothing fancy: the traditional PyPA flow on the hatchling_ build
backend, with uv_ doing the driving and towncrier_ generating the
changelog.

1. collect news fragments: user-facing changes should land with a
   small ``.rst`` snippet under ``nooz/`` (see ``nooz/HOWTO.rst``;
   fragment types are ``feature``, ``bugfix``, ``doc`` and
   ``trivial``),

2. render them into ``NEWS.rst``::

       uvx towncrier build --version <version>

3. build and upload (testpypi first if you're being careful)::

       uv build
       uvx twine upload -r testpypi dist/*
       uvx twine upload dist/*

How you organize built artifacts under ``dist/`` locally (per
release sub-dirs and such) is entirely up to you.

Keep in mind that PyPi releases tend to lag the ``main`` branch
since we develop in the open — ``main`` is usually the thing to
run when you want the latest.

Debugging and monitoring actor trees
------------------------------------

Your "what is my tree doing right now?" toolbox, in escalation
order:

**Live process-tree view** — keep a ``watch``-ed ``pstree``
running in a side terminal; actor procs are recognizable by their
``_subactor[<name>@<uuid-prefix>]`` process titles. The exact
one-liners (plus the ``pgrep`` marker recipes) live in
:doc:`/guide/testing`.

**SIGUSR1 task-tree dumps** — boot any tree with
``enable_stack_on_sig=True`` (or export
``TRACTOR_ENABLE_STACKSCOPE=1``) and every actor installs a
stackscope_ signal handler. Then from any shell::

    # dump every actor's live trio task tree:
    pkill --signal SIGUSR1 -f <part-of-your-cmd>

    # or for a single process:
    kill -SIGUSR1 $(pgrep -f <part-of-your-cmd>)

Each dump is also tee'd (append-mode) to
``/tmp/tractor-stackscope-<pid>.log`` so you still get output
under pytest capture or in CI. This works *without* debug-mode
being enabled — it's the lightest-weight hang-investigation tool
in the box.

**The built-in multi-process debugger** — ``debug_mode=True``
plus :func:`tractor.pause` and friends: the heavyweight champ for
interactive, REPL-driven inspection of a whole tree (including
crash handling). Remember pytest capture interplay — see
:doc:`/guide/testing`.

**Post-mortem zombie sweeps** — ``scripts/tractor-reap`` for the
(should-be-rare!) cases where hacking on the runtime itself wedges
a tree: a SIGINT-first, structured concurrency (SC) polite
escalation, plus ``--shm`` and ``--uds`` leaked-resource sweeps.

Using the log system to trace ``trio`` task flow
------------------------------------------------

The logging system is oriented around the **stack "layers" of the
runtime**, letting you trace logical abstraction layers in the
code — errors, cancellation, IPC and streaming, the low level
transport and wire protocols — independently of one another.

Concretely, ``tractor.log.get_logger()`` returns a
``StackLevelAdapter`` sporting extra level-methods beyond the
stdlib set, including:

- ``.cancel()`` — cancellation-machinery flow,

- ``.runtime()`` — actor-runtime lifecycle chatter,

- ``.devx()`` — debugger/devx tooling internals,

- ``.transport()`` — wire-level msging events.

To get console output at any level from your own code::

    from tractor.log import get_console_log
    get_console_log('cancel')

or, runtime-wide without touching code, just export
``TRACTOR_LOGLEVEL=cancel`` (the env-var wins over caller-passed
levels; great for test runs).

When you want only *one subsystem* cranked, the suite's ``--tl``
flag (and ``tractor.log.apply_logspec()``) accept a per-sublogger
spec::

    uv run pytest tests/... --tl 'devx:runtime,trionics:cancel'

Every record's header includes the emitting actor and task names,
so cross-process flows can be stitched back together by eyeball
(or grep).

Building these docs
-------------------
The site you're reading is sphinx_ + the pydata-sphinx-theme_,
with diagrams authored in d2_ (via our local ``.. d2::``
directive) and *every* code block ``literalinclude``-d straight
from ``examples/`` — so what you read is what CI runs, and it
can't rot.

The one-liner, from any dev shell::

    uv run --group docs make -C docs html

then open ``docs/_build/html/index.html``.

**Nix users**: the d2_ diagram renderer is deliberately kept
*out* of the default dev-shell so casual envs stay lean; it
lives in an opt-in ``docs`` shell::

    # enter the docs shell (puts `d2`, `uv` + python on PATH)
    nix develop .#docs

    # ...then build (diagrams re-render from docs/diagrams/*.d2)
    uv run --group docs make -C docs html

or as a one-shot without staying in the shell::

    nix develop .#docs -c uv run --group docs make -C docs html

**Live-reload** while editing — rebuild + browser refresh on
every save::

    nix develop .#docs -c uv run --with sphinx-autobuild \
        --group docs sphinx-autobuild docs docs/_build/html
    # then open http://127.0.0.1:8000

**Share on your LAN**: bind the server to all interfaces
(``--host 0.0.0.0``) instead of localhost, then hand a peer on
your subnet ``http://<your-lan-ip>:8000``::

    nix develop .#docs -c uv run --with sphinx-autobuild \
        --group docs sphinx-autobuild docs docs/_build/html \
        --host 0.0.0.0 --port 8000

or statically serve an already-built tree (no rebuild-on-save)::

    python -m http.server -d docs/_build/html --bind 0.0.0.0 8000

``hostname -I`` prints the IP to share; note it's an
unauthenticated server bound to every interface, so keep it to a
trusted LAN.

How the diagrams resolve,

- ``.d2`` sources live in ``docs/diagrams/``; their rendered
  SVGs are git-committed under ``docs/_diagrams/`` as a fallback,
- with a ``d2`` binary on ``PATH`` (the ``docs`` shell, or set
  ``D2_BIN='nix run nixpkgs#d2 --'``) any stale SVG re-renders at
  build time,
- with NO binary the committed SVGs are served as-is, so CI and
  casual builds need no ``d2`` at all,
- a ``.d2`` that *fails to compile* is a hard build error under
  ``sphinx-build -W`` (the last-good committed SVG is left
  untouched).

The build is currently **warning-free**; keep it that way — the
``-W`` flag turns any sphinx warning into a hard failure::

    uv run --group docs sphinx-build -b html -W docs docs/_build/html

A terser command cheat-sheet also lives at
``notes_to_self/howtodocs.md``.

.. seealso::
   - :doc:`/guide/testing` — running the suite, watching trees
     live, examples-as-tests conventions and the zombie-reaper.
   - :doc:`/guide/discovery` — the registrar mechanics you'll
     bump into when running multiple trees on one host.

.. _hatchling: https://hatch.pypa.io/latest/
.. _uv: https://docs.astral.sh/uv/
.. _towncrier: https://towncrier.readthedocs.io/en/stable/
.. _stackscope: https://github.com/oremanj/stackscope
.. _sphinx: https://www.sphinx-doc.org/
.. _pydata-sphinx-theme: https://pydata-sphinx-theme.readthedocs.io/
.. _d2: https://d2lang.com
