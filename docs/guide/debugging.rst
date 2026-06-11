.. _debugging:

================================
"Native" multi-process debugging
================================

``tractor`` ships the thing every ``multiprocessing`` user has
wished for and quietly assumed was impossible: a multi-process
debugger that *just works*.

Drop ``await tractor.pause()`` — or, with `greenback`_ installed,
a plain builtin ``breakpoint()`` — anywhere in any actor: the
root, a child, a grandchild, a sync helper function, even an
``asyncio`` task inside an "infected" actor. A full-featured
`pdbp`_ REPL opens *in that process*, with syntax-highlighted
source listings, tab completion and sticky mode, attached to your
one terminal.

Under the hood every REPL entry acquires a tree-global tty mutex
via an IPC request to the root actor, so prompts from concurrent
pauses and crashes never interleave. ``ctrl-c`` is shielded while
any REPL is live, so a stray ``SIGINT`` can't vaporize the tree
out from under you. And in debug mode any uncaught error drops
you into a crash REPL *first in the failing child*, then again at
each parent as the boxed :class:`~tractor.RemoteActorError` climbs
the supervision tree.

No remote-pdb sockets, no ``set_trace()`` port juggling, no
``ptrace`` attach dance: the debugger semantics you already know,
transparently extended across an entire process tree. Because
``tractor`` is a `structured concurrency`_ (SC) runtime, the
debugger composes with supervision instead of fighting it — quit
a REPL and errors keep propagating exactly like `trio`_ taught
you, ending in clean, zombie-free teardown.

We're pretty sure it's the (first ever?) "native" debugging UX
for multi-process Python B)

Enabling debug mode
-------------------

Pass ``debug_mode=True`` to your runtime entrypoint, either
:func:`tractor.open_nursery` (which forwards it to the implicitly
opened root actor) or :func:`tractor.open_root_actor` directly:

.. code:: python

    async with tractor.open_nursery(
        debug_mode=True,    # arm the whole actor tree
    ) as an:
        ...

This arms the debug machinery *tree-wide*:

- crash handling is enabled in every actor: uncaught errors enter
  a REPL before they propagate,
- the internal tty-lock module is auto-exposed over RPC to every
  subactor (this is what makes the one-terminal handoff work),
- console logging is bumped to include ``PDB``-level status msgs
  so you can see REPL acquire/release events as they happen.

You can instead flip it on for just one child, letting its
siblings crash-and-burn the normal way:

.. code:: python

    portal = await an.start_actor(
        'sketchy_worker',
        debug_mode=True,    # OR-ed with the tree-wide flag
    )

See ``examples/debugging/per_actor_debug.py`` for a runnable
proof of the selective style.

.. note::

   Debug mode requires the child-side runtime to be
   ``trio``-native so that the tty-lock IPC dialog works; it's
   currently supported on the ``'trio'`` (default) and
   ``'main_thread_forkserver'`` spawn backends and raises
   ``RuntimeError`` for any other ``start_method``.

Your first pause point
----------------------

:func:`tractor.pause` is the SC-aware, multi-process spelling of
the stdlib's ``breakpoint()``. In the root actor it looks almost
boring:

.. literalinclude:: ../../examples/debugging/root_actor_breakpoint.py
   :caption: examples/debugging/root_actor_breakpoint.py
   :language: python

Run it and you get a ``(Pdb+)`` prompt parked on the ``pause()``
line; type ``c`` (continue) and the program finishes normally.

The exact same call works from *any* subactor, no matter how deep
in the tree:

.. literalinclude:: ../../examples/debugging/subactor_breakpoint.py
   :caption: examples/debugging/subactor_breakpoint.py
   :language: python

Each loop iteration the child actor requests the terminal from
the root over IPC, REPLs you, then releases it on ``c``. Pause
points are re-entrant-safe: repeat calls from the same task are
no-op'd and other local tasks queue politely for the REPL.

When you get bored, type ``q`` (quit): the resulting
``bdb.BdbQuit`` is boxed and shipped to the parent like any other
remote error XD — causality is preserved even for your debugging
mistakes.

Crash REPLs: errors climb the tree
----------------------------------

Pause points are only half the story. With debug mode armed, any
*uncaught* error anywhere in the tree triggers what we call crash
handling mode:

.. literalinclude:: ../../examples/debugging/subactor_error.py
   :caption: examples/debugging/subactor_error.py
   :language: python

What happens when the child hits that (very intentional)
``NameError``:

1. a REPL opens **in the crashed child first** — you inspect the
   raising frame, its locals, the works, right inside the failed
   process,
2. when you quit, the error is boxed into a
   :class:`~tractor.RemoteActorError` and relayed to the parent,
3. the parent (here the root) gets *its own* crash REPL with the
   rendered remote traceback,
4. quit again and the nursery tears the tree down — errors keep
   propagating per SC rules, no zombies left behind.

You debug the failure at every hop of the supervision tree, which
for multi-hop trees means you can chase an error from the leaf
that raised it all the way up to the root that supervises it.

Need to skip REPL entry for certain exceptions? Pass a predicate
via ``open_root_actor(debug_filter=...)``; by default
cancellation-only exception (groups) don't engage the REPL.

One terminal, many actors
-------------------------

So how do N processes share one tty without garbling it? The root
actor owns stdio for the whole tree and guards it with a FIFO
mutex; every subactor REPL entry is an IPC lock request to the
root. Exactly one actor-task in the entire tree can own the
terminal at a time, so prompts never interleave — ever.

.. d2:: diagrams/debug_lock.d2
   :caption: Every REPL entry serializes through the root actor's
       tty lock; ``continue``-ing one REPL hands the terminal to
       the next waiter, FIFO style.
   :alt: sequence diagram of two subactors serializing pdb REPL
       access through the root actor's tty lock

The runtime's teardown paths cooperate too: a cancelling parent
always waits for any live REPL to release before reaping
children, so the debugger never gets yanked out from under you
mid-keystroke.

.. margin:: Watch the tree live

   Run any of these examples with a process-tree watcher in a
   second terminal and watch actors come and go::

       watch -n 0.1 "pstree -a $$"

Here's the showpiece: one daemon child re-entering
``tractor.pause()`` forever inside a stream, while its sibling
repeatedly raises a ``NameError``:

.. literalinclude:: ../../examples/debugging/multi_daemon_subactors.py
   :caption: examples/debugging/multi_daemon_subactors.py
   :language: python

What you'll actually see
************************

Running it looks *roughly* like this (uids, tracebacks and source
listings elided; REPL order can vary with who wins the lock
race)::

    $ python examples/debugging/multi_daemon_subactors.py

    Opening a pdb REPL in paused actor: ('bp_forever', '<uuid>')
    <highlighted source around the `await tractor.pause()` line>
    (Pdb+) c

    Opening a pdb REPL in crashed actor: ('name_error', '<uuid>')
    <live traceback: NameError: name 'doggypants' is not defined>
    (Pdb+) q

    Opening a pdb REPL in crashed actor: ('root', '<uuid>')
    <boxed RemoteActorError traceback relayed from 'name_error'>
    (Pdb+) q

Two (then three) processes, one terminal, zero confusion:
``c``-ing out of the paused daemon's REPL releases the tty lock,
which immediately hands the prompt to the crashed sibling; quit
that and the error propagates as a fully-rendered
:class:`~tractor.RemoteActorError` to the parent where one final
crash REPL catches it before clean, zombie-free teardown.

For maximum drama run
``multi_nested_subactors_error_up_through_nurseries.py`` (under
``examples/debugging/``) which pulls the same trick across a
*three-deep* process tree — the tty lock keeps every prompt
orderly the whole way up.

Post-mortem, on demand
----------------------

Crash handling is automatic, but you can also enter a REPL on
a live exception *manually* with :func:`tractor.post_mortem` —
the actor-aware equivalent of ``pdb.post_mortem()`` — from inside
any ``except`` block in any actor (kwargs: ``tb=`` for an
explicit traceback, plus ``shield=`` and ``hide_tb=``):

.. literalinclude:: ../../examples/debugging/pm_in_subactor.py
   :caption: examples/debugging/pm_in_subactor.py
   :language: python

This example demos three REPL entries from one error:

- the child's manual ``post_mortem()`` inside its ``except``,
- the runtime's automatic crash handler in the same child once
  the error re-raises out of the RPC task,
- a manual ``post_mortem()`` in the parent on the received
  :class:`~tractor.RemoteActorError`, whose ``.boxed_type``
  faithfully reports the original ``NameError``.

Pausing from sync code
----------------------

No ``await``? No problem. :func:`tractor.pause_from_sync` brings
the same tree-aware REPL to plain synchronous functions — handy
when the suspect code is three helpers deep and decidedly not
async.

It's powered by `greenback`_, which is optional, so you need to:

1. install it (it ships in ``tractor``'s ``sync_pause``
   dependency group),
2. enable it at runtime entry:

.. code:: python

    async with tractor.open_nursery(
        debug_mode=True,
        maybe_enable_greenback=True,
    ) as an:
        ...

With that armed, sync code can pause from three different caller
environments: the main ``trio`` thread, ``trio.to_thread`` bg
threads, and (see the next section) ``asyncio`` tasks in infected
actors. The greenback "portal" hops back into the ``trio`` loop
to do the lock/REPL dance on your behalf:

.. literalinclude:: ../../examples/debugging/sync_bp.py
   :caption: examples/debugging/sync_bp.py (the sync fn, excerpt)
   :language: python
   :pyobject: sync_pause

.. literalinclude:: ../../examples/debugging/sync_bp.py
   :caption: examples/debugging/sync_bp.py (called in a subactor,
       excerpt)
   :language: python
   :pyobject: start_n_sync_pause

The full script also exercises the hairier root-actor bg-thread
cases (and documents their remaining sharp edges) if you want the
deep lore.

The builtin ``breakpoint()`` override
*************************************

When debug mode boots with greenback available, ``tractor`` wires
Python's `PEP 553`_ hook so the *builtin* ``breakpoint()`` becomes
the actor-aware sync pause, by exporting::

    PYTHONBREAKPOINT=tractor.devx.debug._sync_pause_from_builtin

That means third-party and legacy code containing bare
``breakpoint()`` calls debugs correctly inside your actor tree
with zero edits (the override even forwards kwargs like
``hide_tb`` to the underlying pause machinery, as shown in the
excerpt above).

.. warning::

   Without greenback (or with ``maybe_enable_greenback=False``,
   the default), ``debug_mode=True`` instead *blocks* the builtin
   ``breakpoint()``: ``sys.breakpointhook`` is swapped for a
   raiser and ``PYTHONBREAKPOINT=0`` is set. A naive
   ``breakpoint()`` from some random process would clobber the
   shared tty, so we'd rather hand you a loud ``RuntimeError``
   with install instructions.

Both the hook and the env var are restored to their prior values
on runtime exit — see
``examples/debugging/restore_builtin_breakpoint.py`` for the
proof.

Breakpoints inside ``asyncio`` tasks
------------------------------------

Yes, even "infected ``asyncio``" actors get the goods. Spawn a
child with ``infect_asyncio=True`` (``trio`` runs as a guest on
the ``asyncio`` loop inside it) and, with debug mode + greenback
armed, every ``asyncio`` task started via ``tractor.to_asyncio``
is automatically granted a greenback portal — so a plain builtin
``breakpoint()`` (or ``tractor.pause_from_sync()``) inside an
``asyncio.Task`` joins the same single-terminal, tree-locked REPL
flow:

.. literalinclude:: ../../examples/debugging/asyncio_bp.py
   :caption: examples/debugging/asyncio_bp.py
   :language: python

Note the interleave: a ``breakpoint()`` on the ``asyncio`` side,
``tractor.pause()`` on the ``trio`` side of the same actor, and
another pause up in the root — all serialized through the one tty
lock with no cross-actor (or cross-event-loop!) clobbering.

One catch: ``asyncio`` tasks spawned *out-of-band* — i.e. not via
``tractor.to_asyncio``, typically by some third-party aio lib —
have no portal bestowed, so a sync pause from one raises a loud
``RuntimeError`` telling you to ``greenback.ensure_portal()``
first. See :ref:`the caveats <debugging-caveats>` below.

Teardown debugging: the shielded pause
--------------------------------------

`Cancellation`_ is ``trio``'s bread and butter, which raises an
awkward question: how do you REPL inside an *already-cancelled*
scope, say while debugging some teardown sequence? A bare
``pause()`` would itself be cancelled at its next checkpoint.

The answer is ``await tractor.pause(shield=True)``, which wraps
the lock acquisition and REPL session in a shielded cancel scope
(``post_mortem(shield=True)`` works the same way):

.. literalinclude:: ../../examples/debugging/shielded_pause.py
   :caption: examples/debugging/shielded_pause.py
   :language: python

If you forget, ``tractor`` has your back: an unshielded
``pause()`` from a cancelled scope fails fast with a hint
suggesting ``await tractor.pause(shield=True)`` instead of
silently never REPL-ing.

Go ahead, mash ctrl-c
---------------------

While any REPL is live the runtime installs a custom ``SIGINT``
handler tree-wide so that a reflexive ``ctrl-c`` (or five) can't
nuke your debug session:

- the actor that owns the REPL ignores the interrupt and simply
  re-flushes the prompt — keep mashing, it's fine,
- the root actor ignores ``SIGINT`` while a still-IPC-connected
  child holds the tty lock, so the supervisor won't tear down the
  tree out from under the debugger,
- if the lock state has gone *stale* — the locking child died or
  its IPC channel dropped — the root cancels the stale lock scope
  and restores ``trio``'s default handler, so ``ctrl-c`` works
  again exactly when it should.

The handler is uninstalled and ``trio``'s own ``SIGINT``
semantics restored every time a REPL releases (on ``continue`` /
``quit``).

Live task-tree dumps
--------------------

Sometimes there's no error to catch — the tree is just *hung* and
you want to know where. For that ``tractor`` integrates
`stackscope`_: send a signal, get a full ``trio`` task-tree dump
from every actor in the tree.

Enable it any of three ways:

- ``open_root_actor(enable_stack_on_sig=True)`` (or via
  ``open_nursery()`` which forwards it),
- set ``TRACTOR_ENABLE_STACKSCOPE=1`` in the env — it's inherited
  through the process tree so every (sub)actor arms the handler
  at boot,
- call ``tractor.devx.enable_stack_on_sig()`` directly.

It's intentionally *not* gated on ``debug_mode`` so you can leave
it armed in plain runs. Then, when the hang strikes, signal the
tree with ``SIGUSR1``.

.. tip::

   No need to hunt down pids — pattern-match the original cmdline
   with ``pkill``::

       $ pkill --signal SIGUSR1 -f "python example_script.py"

Each actor dumps its entire ``trio`` task tree (full nursery
recursion via ``stackscope.extract()``) to its tty *and* tees it
to ``/tmp/tractor-stackscope-<pid>.log`` — so the trace survives
even under captured-stdio harnesses — then relays the signal on
to its children, parent-before-child, until the whole tree has
reported in.

Try it yourself with the demo script, which deliberately hangs a
subactor in a shielded sleep:

.. literalinclude:: ../../examples/debugging/shield_hang_in_sub.py
   :caption: examples/debugging/shield_hang_in_sub.py
   :language: python

(That ``trio.CancelScope(shield=True)`` hang also shows off the
zombie reaper: ``ctrl-c`` the root and the un-cancellable child
still gets hard-reaped — if you can create a zombie it **is a
bug**.)

Crash handling for sync and CLI code
------------------------------------

All of the above rides on the actor runtime, but crashes don't
politely wait for ``trio.run()``. For plain sync code — think
``typer``/``click`` CLI endpoints, config parsing, anything
pre-runtime — there's a sync context manager that wraps the same
``pdbp`` post-mortem UX:

.. code:: python

    from tractor.devx import open_crash_handler

    def main():    # any sync code, no runtime required
        with open_crash_handler() as boxed:
            run_my_cli_thing()

By default any ``BaseException`` (minus an ``ignore`` set
defaulting to ``KeyboardInterrupt`` and ``trio.Cancelled``)
enters the REPL then re-raises on exit; pass
``raise_on_exit=False`` to suppress instead and introspect the
``boxed.value`` afterward. The ``catch``/``ignore`` sets and a
``repl_fixture`` are all tweakable.

For the classic ``--pdb`` CLI-flag pattern use the conditional
variant:

.. code:: python

    from tractor.devx import maybe_open_crash_handler

    @app.command()    # a `typer` (or `click`) endpoint
    def cmd(pdb: bool = False):
        with maybe_open_crash_handler(pdb=pdb):
            ...

REPL niceties and hooks
-----------------------

Every REPL in this guide is a `pdbp`_ instance (the maintained
fork-and-fix of `pdb++`_) pre-configured by ``tractor``:

- pygments syntax highlighting in listings and tracebacks,
- tab completion — including an automatic fixup for
  libedit-compiled CPythons (e.g. ``uv``-distributed pythons),
- sticky mode available via the ``sticky`` command (off by
  default),
- no long-line truncation (terminal resizes behave),
- the ``(Pdb+)`` prompt, ``ll``, hidden-frames support and the
  rest of the ``pdb++`` goodies you may already know.

Internal runtime frames are traceback-hidden so the REPL lands
exactly on *your* ``pause()``-call or crash frame, never on
``tractor`` guts.

Finally, if your app owns the terminal (TUIs, fullscreen
dashboards) pass ``repl_fixture=<your ctx mngr>`` to ``pause()``,
``post_mortem()`` or ``open_crash_handler()``: it's entered just
before the REPL engages (return ``False`` to skip entry entirely)
and exited on release — perfect for suspending and restoring your
screen around a debug session.

.. _debugging-caveats:

Caveats and platform notes
--------------------------

An honest list of the current rough edges:

- **Windows**: the debugger has no CI coverage on windows at all
  (the entire test module is skipped there); manual testing has
  shown it *can* work, but you're in uncharted territory —
  reports welcome!
- **macOS**: supported but with rough edges: special-cased prompt
  re-flushing for ``bash``-on-darwin, a few tooling tests skipped
  on CI, and the AF_UNIX ~104-char socket-path limit forces some
  examples (like the stackscope demo above) to fall back from
  ``'uds'`` to ``'tcp'`` transport. Wonder if all of it'll work
  on OS X? So do we.
- **CPython 3.14**: ``greenback`` (via ``greenlet``) doesn't
  support 3.14 yet, so ``pause_from_sync()`` and the builtin
  ``breakpoint()`` override are effectively 3.13-only for now.
  The async APIs — ``pause()`` and ``post_mortem()`` — need no
  greenback and work everywhere.
- **out-of-band** ``asyncio`` **tasks**: sync pauses from aio
  tasks *not* spawned via ``tractor.to_asyncio`` raise a
  ``RuntimeError`` (no greenback portal was bestowed); run
  ``await greenback.ensure_portal()`` inside such a task first.
- **nested-tree ctrl-c edges**: ``SIGINT`` relay through
  intermediary parents that aren't themselves in debug mode still
  has known rough edges — see `#320`_.
- **captured stdio**: ``pytest``-style output capture can hang a
  ``pause()``; use a real terminal (or a pty à la ``pexpect``,
  which is how ``tractor``'s own suite drives every one of these
  examples).

Where to next?
--------------

.. seealso::

   - :doc:`/guide/context` — the SC-linked cross-actor task API
     that all the crash-propagation semantics above ride on.
   - :func:`tractor.pause`, :func:`tractor.post_mortem` and
     :func:`tractor.pause_from_sync` in the API reference.
   - ``examples/debugging/`` — 20-odd runnable scripts, nearly
     every one exercised by the test suite through a real pty.

.. _structured concurrency:
   https://en.wikipedia.org/wiki/Structured_concurrency
.. _trio: https://github.com/python-trio/trio
.. _cancellation: https://trio.readthedocs.io/en/latest/
   reference-core.html#cancellation-and-timeouts
.. _pdbp: https://github.com/mdmintz/pdbp
.. _pdb++: https://github.com/pdbpp/pdbpp
.. _greenback: https://github.com/oremanj/greenback
.. _stackscope: https://github.com/oremanj/stackscope
.. _PEP 553: https://peps.python.org/pep-0553/
.. _#320: https://github.com/goodboy/tractor/issues/320
