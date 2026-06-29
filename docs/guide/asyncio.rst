Infected ``asyncio``
====================
``tractor`` is "just trio_", but the Python world is packed with
libraries that only speak ``asyncio``: websocket stacks, vendor
SDKs, that one exchange client you can't route around. Rather than
make you rewrite them, ``tractor`` lets you *quarantine* them inside
a dedicated subactor which runs both event loops at once, with full
`structured concurrency`_ (SC) guarantees maintained across the
loop boundary *and* the process tree.

In the project's own words:

    Yes, we spawn a python process, run ``asyncio``, start ``trio``
    on the ``asyncio`` loop, then send commands to the ``trio``
    scheduled tasks to tell ``asyncio`` tasks what to do XD

We call this "infected ``asyncio``" mode: the subactor's stdlib
loop runs as the *host* with ``trio`` embedded on top in `guest`_
mode, and your ``trio`` tasks drive ``asyncio`` tasks through
a linked, SC-supervised, in-memory channel.

.. d2:: diagrams/infected_aio.d2
   :caption: One process, two schedulers: ``trio`` rides the
       ``asyncio`` loop as a guest while the parent speaks plain
       ``tractor`` IPC, none the wiser.
   :alt: parent actor connected over IPC to a subactor whose
       asyncio loop hosts trio in guest mode, with a
       LinkedTaskChannel pairing a trio task to an asyncio task

.. note::

   Infected ``asyncio`` mode is **experimental**: it works (we
   beat on it plenty) but parts of the API surface and some
   edge-case semantics are still settling. Got opinions on the
   interop design? Feel free to sling them in `#273`_!

How the infection takes hold
----------------------------
A normal subactor boots by running the ``tractor`` runtime's task
tree directly under ``trio.run()``. Pass ``infect_asyncio=True``
at spawn time and the child's entrypoint changes shape entirely:

1. the process starts the stdlib loop via ``asyncio.run()``,
2. the first ``asyncio`` task calls
   ``trio.lowlevel.start_guest_run()``, embedding the ``trio``
   scheduler *inside* the already running ``asyncio`` loop (the
   upstream `guest`_-mode feature),
3. the regular ``tractor`` runtime then boots on the guest
   ``trio`` side and connects back to its parent like any other
   subactor.

.. margin:: Symptoms

   Looks like your stdlib event loop has caught a case of "the
   trios"! Don't worry, you'll barely notice; and if anything
   gets too bad, your parents will know about it B)

Both schedulers interleave in a single thread, no GIL gymnastics
required. From the rest of the actor tree the infected child is
indistinguishable from any other actor: same IPC protocol, same
supervision and cancellation semantics, same zombie-safety
guarantees. The difference is purely internal: ``trio`` tasks in
that process can start and drive ``asyncio`` tasks through the
``tractor.to_asyncio`` API.

Spawning an infected subactor
-----------------------------
Just flip the flag on :meth:`tractor.ActorNursery.start_actor`:

.. code:: python

    async with tractor.open_nursery() as an:
        portal = await an.start_actor(
            'aio_side',
            enable_modules=[__name__],
            infect_asyncio=True,
        )

The one-shot convenience ``ActorNursery.run_in_actor()`` accepts
the same flag. The ``to_asyncio`` APIs may **only** be called from
tasks inside an infected actor; calling them anywhere else raises
a loud ``RuntimeError``. You can introspect at runtime with
``tractor.current_actor().is_infected_aio()``.

Linking tasks with ``open_channel_from()``
------------------------------------------
The core primitive is :func:`tractor.to_asyncio.open_channel_from`,
an async context manager which starts your ``asyncio`` function as
a real ``asyncio.Task`` and yields a two-way channel linking it to
the calling ``trio`` task:

.. code:: python

    from tractor import to_asyncio

    async with to_asyncio.open_channel_from(
        aio_main,        # async def aio_main(chan, **kwargs)
        period=0.5,      # extra kwargs are passed through
    ) as (chan, first):
        await chan.send('tick')

The semantics deliberately mirror the inter-actor ``Context``
handshake from :doc:`/guide/context`:

- the target fn must declare a parameter literally named ``chan``;
  the runtime injects the shared
  :class:`~tractor.to_asyncio.LinkedTaskChannel` by keyword.
- the ``trio`` side blocks at entry until the ``asyncio`` task
  calls ``chan.started_nowait(value)``; that value is delivered as
  ``first``, exactly like the ``(ctx, first)`` pair you get from
  ``Portal.open_context()`` after the child calls
  ``ctx.started()``.
- a first value **must** be sent from the ``asyncio`` side or the
  ``trio`` side will never unblock.
- on block exit the pair is torn down *together*; neither task can
  outlive the other (more on this below).

A full example: the echo server
-------------------------------
Here's the canonical demo, a round-trip echo service where the
``asyncio`` task is told what to do by a ``trio`` task which is in
turn driven over IPC by the root actor:

.. literalinclude:: ../../examples/infected_asyncio_echo_server.py
   :caption: examples/infected_asyncio_echo_server.py
   :language: python

What's going on?

- there are three task layers: the root actor's pure ``trio``
  task, the infected child's ``trio``-side ``@tractor.context``
  endpoint (``trio_to_aio_echo_server()``), and the child's
  ``asyncio`` task (``aio_echo_server()``).
- two ``started``-style handshakes compose: the aio task's
  ``chan.started_nowait('start')`` unblocks the child's
  ``open_channel_from()`` entry, then the child relays that same
  value up via ``await ctx.started(first)`` which unblocks the
  root's ``open_context()`` entry. Synchronization all the way
  down, er, up.
- each round trip flows: root ``stream.send()`` -> IPC -> child
  ``async for msg in stream`` -> ``chan.send(msg)`` -> aio
  ``await chan.get()`` -> ``chan.send_nowait()`` -> child
  ``chan.receive()`` -> ``stream.send(out)`` -> IPC -> root.
- when the root breaks out of its stream loop and exits the
  context block, the child's stream ends, its channel block exits,
  and the ``asyncio`` task is reaped along with it; the final
  ``portal.cancel_actor()`` then tears down the whole process. No
  orphaned ``asyncio`` tasks, no zombie procs; if you manage to
  create either it **is a bug**.

``LinkedTaskChannel``: one channel, two sides
---------------------------------------------
The same channel object is shared by both tasks; which methods you
call depends on which loop schedules your task. The ``trio`` side
gets a standard ``trio.abc.Channel`` interface while the
``asyncio`` side gets queue-flavored, mostly-sync methods:

.. list-table::
   :header-rows: 1
   :widths: 14 36 50

   * - side
     - call
     - what it does
   * - ``trio``
     - ``await chan.send(item)``
     - ship ``item`` to the ``asyncio`` task (enqueues onto an
       internal ``asyncio.Queue``).
   * - ``trio``
     - ``await chan.receive()``
     - wait for the next value from the ``asyncio`` side; the
       channel also supports ``async for``.
   * - ``trio``
     - ``await chan.wait_for_result()``
     - block until the ``asyncio`` task completes; return its
       final result or raise its (translated) error.
   * - ``trio``
     - ``chan.subscribe()``
     - acm yielding a ``BroadcastReceiver`` so N local tasks can
       each consume a copy of the inbound stream (see below).
   * - ``trio``
     - ``chan.cancel_asyncio_task()``
     - explicitly request cancellation of the linked ``asyncio``
       task.
   * - ``asyncio``
     - ``chan.started_nowait(value)``
     - deliver the "first" value; unblocks the ``trio`` side's
       ``open_channel_from()`` entry (mirrors ``ctx.started()``).
   * - ``asyncio``
     - ``await chan.get()``
     - wait for the next value sent from the ``trio`` side.
   * - ``asyncio``
     - ``chan.send_nowait(item)``
     - push a value to the ``trio`` side without blocking.

Fan-out with ``.subscribe()``
*****************************
Just like :meth:`tractor.MsgStream.subscribe` does for IPC
streams, ``chan.subscribe()`` lets multiple local ``trio`` tasks
each receive *every* value sent from the single ``asyncio`` task:

.. code:: python

    async with chan.subscribe() as bcast:
        async for msg in bcast:
            ...

The underlying broadcast machinery is lazily allocated on first
use and is *not* reversible for the channel's remaining lifetime,
so only reach for it when you actually want the fan-out.

One-shot calls with ``run_task()``
----------------------------------
When you just want a single ``asyncio`` result and no streaming
dialog, skip the channel ceremony and use
:func:`tractor.to_asyncio.run_task`:

.. code:: python

    import asyncio
    from tractor import to_asyncio

    async def aio_fetch(url: str) -> str:
        await asyncio.sleep(0.3)   # pretend-IO, aio style
        return f'<html>sup {url}</html>'

    # from any trio task inside the infected actor:
    page = await to_asyncio.run_task(aio_fetch, url='https://x.io')

It schedules the fn as an ``asyncio.Task``, waits for completion
and hands the return value back to ``trio``; think of it as the
cross-loop sibling of ``ActorNursery.run_in_actor()``. Errors and
cancellation are translated exactly as for channels.

Cross-loop errors and cancellation
----------------------------------
The paired tasks are *SC linked*: exception and cancel handling
tears down **both** sides on any unexpected error or cancellation,
in either loop. There is no fire-and-forget mode; a
``LinkedTaskChannel`` is a supervision scope just like a
``Context`` is across processes.

Because each loop has its own (incompatible) cancellation and exit
machinery, boundary crossings are translated into dedicated
exception types, all importable from ``tractor.to_asyncio``:

.. list-table::
   :header-rows: 1
   :widths: 26 22 52

   * - exception
     - raised in
     - meaning
   * - ``AsyncioCancelled``
     - the ``trio`` task
     - the linked ``asyncio`` task was cancelled by itself or
       a 3rd party (i.e. *not* by the ``trio`` side).
   * - ``AsyncioTaskExited``
     - the ``trio`` task
     - the ``asyncio`` task returned/exited early while the
       ``trio`` side still held the link open.
   * - ``TrioCancelled``
     - the ``asyncio`` task
     - the ``trio`` side was cancelled (or crashed) so the
       ``asyncio`` task is being torn down per SC rules.
   * - ``TrioTaskExited``
     - the ``asyncio`` task
     - the ``trio`` side exited gracefully while the ``asyncio``
       task was still running; a "clean shutdown" signal much
       like closing a ``trio`` mem-chan.

By default ``open_channel_from(suppress_graceful_exits=True)``
absorbs the two ``*TaskExited`` signals so happy-path teardown
stays silent; pass ``False`` when your app wants to handle early
peer-exit explicitly.

Past the task pair, everything composes with the normal actor
story: an unhandled ``asyncio`` error is translated into the
``trio`` side, propagates out of your ``@tractor.context``
endpoint, and arrives at the parent boxed as
a :class:`tractor.RemoteActorError`. One SC discipline,
end-to-end, across loops *and* processes.

Breakpoints in ``asyncio`` tasks
--------------------------------
Yes, the multi-actor REPL works here too. With
``debug_mode=True`` enabled on your tree the ``trio`` side of an
infected actor can ``await tractor.pause()`` as usual, and with
greenback enabled (``maybe_enable_greenback=True``) even the
builtin ``breakpoint()`` works from *inside* ``asyncio`` tasks;
see ``examples/debugging/asyncio_bp.py`` for the full tour. The
root-TTY locking dance behind all this is covered in
:doc:`/guide/debugging`.

Where to next?
--------------
.. seealso::

   - :doc:`/guide/context` for the inter-actor handshake and
     streaming APIs which this whole interop layer mirrors.
   - :doc:`/guide/msging` for typing the payloads you shuttle
     between actors (and loops).
   - :doc:`/guide/debugging` for the multi-process REPL that
     keeps working even when your loop has "the trios".

.. _trio: https://github.com/python-trio/trio
.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _guest: https://trio.readthedocs.io/en/stable/reference-lowlevel.html?highlight=guest%20mode#using-guest-mode-to-run-trio-on-top-of-other-event-loops
.. _#273: https://github.com/goodboy/tractor/issues/273
