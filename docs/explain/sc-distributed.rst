Structured concurrency, across processes
=========================================
``tractor`` makes one bet: the discipline that made
``trio``'s concurrency *readable* — `structured
concurrency`_ (SC) — works just as well when the
"tasks" are whole OS processes talking over a wire.
This page distills what that means, from first
principles, with as little ceremony as possible.

.. margin:: The canon

   If SC is new to you, the seminal `blog post`_ is
   still the best hour you'll spend on concurrent
   programming; the `trio docs`_, wikipedia's SC_
   page and the diagrams over at libdill-docs_ round
   it out nicely.

SC in one breath
----------------
Structured concurrency is the rule that **concurrency
gets a scope**: every task is spawned *inside* a block
(a ``trio`` *nursery*) and that block **cannot exit
until every task it spawned has finished** — returned,
errored, or been cancelled.

That one rule buys you the properties you already
rely on in sequential code,

- a function call is a *black box*: when it returns,
  everything it started is **done** — no secret
  background tasks leaking out the sides,
- an exception **always has somewhere to go**: up the
  (task) tree to a parent which is, by construction,
  still there waiting,
- cancellation has a well defined *shape*: cancel a
  scope and it flows down to every task inside it,
  and only those.

In short: your **runtime task tree matches your source
code's indentation**. Concurrency you can read.

The leap: process-shaped tasks
------------------------------
Now swap "task" for "process".

A ``tractor`` *actor* is just a Python process running
its own ``trio.run()`` — its own private task tree,
sharing **nothing** with its siblings. You spawn
actors from an :class:`tractor.ActorNursery`, which
behaves exactly the way the name implies,

.. code:: python

    async with tractor.open_nursery() as an:
        portal = await an.start_actor(
            'worker',
            enable_modules=[__name__],
        )
        ...
    # ^ block exit == every spawned process has
    # completed, errored or been cancelled, and
    # been **reaped**. No exceptions, no zombies.

so the whole program becomes a *tree of process-trees*
— a `supervision tree`_ in erlang-speak — where every
arrow means "spawned by, **waited on by**, and
supervised by".

.. d2:: diagrams/actor_tree.d2
   :caption: A ``tractor`` program: a process tree of
       ``trio`` task trees; every parent **must wait**
       on its children.
   :width: 85%

Causality: no process outlives its parent
-----------------------------------------
The stdlib's ``multiprocessing`` (and most "job
queue" systems) treat child processes as
fire-and-forget by default: orphans, zombies, lost
tracebacks and ``kill -9`` cleanup scripts are *your*
problem. ``tractor`` instead inherits ``trio``'s
`causality`_ discipline,

- **no spawning willy-nilly**: every actor is born
  from a nursery block with a known parent,
- **lifetimes nest**: a sub-actor's entire process
  tree lives strictly inside its parent's nursery
  scope,
- **teardown is guaranteed**: when a scope exits (or
  errors, or is cancelled) the runtime SIGINTs,
  waits, and (only if it must) hard-kills + reaps
  everything underneath.

We take the zombie thing personally: *if you can
create orphaned child processes without using a
system signal, it* **is a bug** — and there's a test
suite to back that sentence up.

Errors always propagate (yes, across the wire)
----------------------------------------------
In ``trio``, an exception in any task tears through
its nursery to a parent that must handle it —
`exceptions always propagate`_. ``tractor`` extends
the same guarantee across process boundaries: an
uncaught error in a remote task is

1. captured + serialized in the child,
2. shipped home over IPC as a typed ``Error`` msg,
3. re-raised in the parent **boxed** as a
   :class:`tractor.RemoteActorError` carrying the
   original type (``.boxed_type``), a rendered remote
   traceback, and the erroring actor's id,

while the supervising nursery applies its (currently
*one-cancels-all*, just like ``trio``) strategy to any
sibling actors. A crash three processes deep arrives
at your shell as one coherent, causal traceback chain
— not a silent dead worker and a stuck queue.

Cancellation is a request, supervision is the rule
--------------------------------------------------
Cancellation likewise keeps ``trio``'s semantics
*verbatim*, just transported: cancelling an actor
nursery (or a single :class:`tractor.Context` between
two tasks in different processes) sends an explicit
cancel **request** over IPC which the remote runtime
translates into a real ``trio`` cancel-scope cancel —
then *acks back* so the requester can await
confirmation within a bounded time. Nothing is ever
"just killed" first; graceful always precedes brutal.

Because every cross-process dialog is a pair of
**linked tasks** — one on each side, each inside its
own cancel scope — SC stays *transitive*: supervision
doesn't stop at the process boundary, it tunnels
through every hop of the tree. The wire protocol that
enforces this (a small set of typed msgs:
``Start``/``Started``/``Yield``/``Stop``/``Return``/
``Error``) is detailed in :doc:`/guide/msging` and
:doc:`/guide/context`.

Hold up, is this an "actor model"?
----------------------------------
Let's stop and ask how many canon actor model papers
you've actually read ;)

From `the author's mouth`_, the **only** requirement
is `adherence to`_ the `3 axioms`_::

    In response to a message, an actor may:

    - send a finite number of new messages
    - create a finite number of new actors
    - designate a new behavior to process subsequent
      messages

``tractor`` adheres — actors exchange msgs, spawn
actors, and swap behaviors — **with no extra API** to
learn. What we *don't* copy is the cultural baggage:
no visible mailboxes, no untyped fire-and-forget
``send()``, no "let it crash" without a supervisor
that actually hears about it, and definitely no
shared-reference *proxy objects* pretending the
network isn't there. If our "actors" don't look like
what you expected, that's **intentional**: being an
actor model is just one property of the system; being
*structured* is the point.

Why processes at all?
---------------------
Python has a GIL; an actor model by definition shares
no state; so the *process* is the natural runtime
unit — you get real multi-core parallelism and hard
memory isolation for free. But the deeper win is
uniformity: because actors only ever talk via msgs
over a :class:`tractor.Channel` (TCP, UDS, more to
come), the **same code** runs your laptop's worker
pool and a multi-host cluster; "distributed" is a
deployment detail, not an API.

It's just ``trio``
------------------
If you remember one framing, make it this: ``tractor``
**is just** ``trio`` — with nurseries that can spawn
processes and streams that can cross them. Same
nursery discipline, same cancellation semantics, same
"how was this not always the API?" feeling, one level
up the process tree.

.. seealso::

   :doc:`/explain/architecture` for how the runtime
   layers deliver all of the above, and
   :doc:`/start/quickstart` to feel it in ~20 lines of
   code.

.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _SC: https://en.wikipedia.org/wiki/Structured_concurrency
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _libdill-docs: https://sustrik.github.io/libdill/structured-concurrency.html
.. _supervision tree: https://www.erlang.org/doc/design_principles/des_princ.html
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _exceptions always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
.. _the author's mouth: https://www.youtube.com/watch?v=7erJ1DV_Tlo&t=162s
.. _adherence to: https://www.youtube.com/watch?v=7erJ1DV_Tlo&t=1821s
.. _3 axioms: https://en.wikipedia.org/wiki/Actor_model#Fundamental_concepts
