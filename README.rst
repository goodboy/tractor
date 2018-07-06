tractor
=======
A minimalist `actor model`_ built on multiprocessing_ and trio_.

``tractor`` is an attempt to take trionic_ concurrency concepts and apply
them to distributed-multicore Python.

``tractor`` lets you run or spawn Python processes which each internally
run a single ``trio`` task tree (also known as an `async sandwich`_) and
which can communicate with each other over channels_ using a transparent
async function calling API called *portals* (a name also borrowed_
from ``trio``).

``tractor``'s tenets non-comprehensively include:

- no spawning of processes *willy-nilly*; causality_ is paramount!
- `shared nothing architecture`_
- remote errors `always propagate`_ back to the caller
- verbatim support for ``trio``'s cancellation_ system
- no use of *proxy* objects to wrap RPC calls
- an immersive debugging experience
- be simple, be small

.. warning:: ``tractor`` is in alpha-alpha and is expected to change rapidly!
    Expect nothing to be set in stone and your ideas about where it should go
    to be greatly appreciated!

.. _trionic: https://trio.readthedocs.io/en/latest/design.html#high-level-design-principles
.. _async sandwich: https://trio.readthedocs.io/en/latest/tutorial.html#async-sandwich
.. _actor model: https://en.wikipedia.org/wiki/Actor_model
.. _always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
.. _cancellation: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-and-timeouts
.. _multiprocessing: https://docs.python.org/3/library/multiprocessing.html
.. _trio: https://github.com/python-trio/trio
.. _channels: https://en.wikipedia.org/wiki/Channel_(programming)
.. _borrowed:
    https://trio.readthedocs.io/en/latest/reference-core.html#getting-back-into-the-trio-thread-from-another-thread
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _shared nothing architecture: https://en.wikipedia.org/wiki/Shared-nothing_architecture


What's this? Spawning event loops in subprocesses?
--------------------------------------------------
Close, but not quite.

The first step to grok ``tractor`` is to get the basics of ``trio``
down. A great place to start is the `trio docs`_ and this `blog post`_
by njsmith_.

``tractor`` takes much inspiration from pulsar_ and execnet_ but attempts to be much more
minimal, focus on sophistication of the lower level distributed architecture,
and of course does **not** use ``asyncio``, hence **no** event loops.

.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _pulsar: http://quantmind.github.io/pulsar/design.html
.. _execnet: https://codespeak.net/execnet/
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _njsmith: https://github.com/njsmith/


A trynamic first scene
----------------------
As a first example let's spawn a couple actors (in separate processes)
and have them run their lines:

.. code:: python

    import tractor
    from functools import partial

    _this_module = __name__
    the_line = 'Hi my name is {}'


    async def hi():
        return the_line.format(tractor.current_actor().name)


    async def say_hello(other_actor):
        await trio.sleep(0.4)  # wait for other actor to spawn
        async with tractor.find_actor(other_actor) as portal:
            return await portal.run(_this_module, 'hi')


    async def main():
        """Main tractor entry point, the "master" process (for now
        acts as the "director").
        """
        async with tractor.open_nursery() as n:
            print("Alright... Action!")

            donny = await n.start_actor(
                'donny',
                main=partial(say_hello, 'gretchen'),
                rpc_module_paths=[_this_module],
                outlive_main=True
            )
            gretchen = await n.start_actor(
                'gretchen',
                main=partial(say_hello, 'donny'),
                rpc_module_paths=[_this_module],
            )
            print(await gretchen.result())
            print(await donny.result())
            await donny.cancel_actor()
            print("CUTTTT CUUTT CUT!!?! Donny!! You're supposed to say...")


    tractor.run(main)


Here, we've spawned two actors, *donny* and *gretchen* in separate
processes. Each starts up and begins executing their *main task*
defined by an async function, ``say_hello()``.  The function instructs
each actor to find their partner and say hello by calling their
partner's ``hi()`` function using a something called a *portal*. Each
actor receives a response and relays that back to the parent actor (in
this case our "director").

To gain more insight as to how ``tractor`` accomplishes all this please
read on!


Actor spawning and causality
----------------------------
``tractor`` tries to take ``trio``'s concept of causal task lifetimes
to multi-process land. Accordingly ``tractor``'s actor nursery behaves
similar to the nursery_ in ``trio``. That is, an ``ActorNursery``
created with ``tractor.open_nursery()`` waits on spawned sub-actors to
complete (or error) in the same causal_ way ``trio`` waits on spawned
subtasks. This includes errors from any one sub-actor causing all other
actors spawned by the nursery to be cancelled_. Eventually ``tractor``
plans to support different `supervision strategies`_ like ``erlang``.

To spawn an actor open a *nursery block* and use the ``start_actor()``
method:

.. code:: python

    def movie_theatre_question():
        """A question asked in a dark theatre, in a tangent
        (errr, I mean different) process.
        """
        return 'have you ever seen a portal?'


    async def main():
        """The main ``tractor`` routine.
        """
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'frank',
                # enable the actor to run funcs from this current module
                rpc_module_paths=[__name__],
                outlive_main=True,
            )

            print(await portal.run(__name__, 'movie_theatre_question'))
            # calls the subactor a 2nd time
            print(await portal.run(__name__, 'movie_theatre_question'))

            # the async with will block here indefinitely waiting
            # for our actor "frank" to complete, but since it's an
            # "outlive_main" actor it will never end until cancelled
            await portal.cancel_actor()

Notice the ``rpc_module_paths`` `kwarg` here, it's a list of module path
strings that will be loaded and made accessible for execution in the
remote actor. For now this is a simple mechanism to restrict the
functionality of the remote actor and uses Python's module system to
define the allowed remote function namespace(s).

Spawned actor lifetimes can be configured in one of two ways:

- the actor terminates when its *main* task completes (the default if
  the ``main`` kwarg is provided)
- the actor can be told to ``outlive_main=True`` and thus act like an RPC
  daemon where it runs indefinitely until cancelled

Had we wanted the former in our example it would have been much simpler:

.. code:: python

    def cellar_door():
        return "Dang that's beautiful"


    async def main():
        """The main ``tractor`` routine.
        """
        async with tractor.open_nursery() as n:
            portal = await n.start_actor('some_linguist', main=cellar_door)

        # The ``async with`` will unblock here since the 'some_linguist'
        # actor has completed its main task ``cellar_door``.

        print(await portal.result())


Note that the main task's *final result(s)* is **always** accessed using
``Portal.result()``.

.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
.. _supervision strategies: http://erlang.org/doc/man/supervisor.html#sup_flags
.. _causal: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#causality
.. _cancelled: https://trio.readthedocs.io/en/latest/reference-core.html#child-tasks-and-cancellation


Transparent function calling using *portals*
--------------------------------------------
``tractor`` currently is experimenting with an *async-native*
IPC API where routines that are invoked in remote *actors* are treated
as though they were invoked locally in the calling actor. So when you
see a call to ``await portal.run()`` what you get back is what you'd expect
to if you'd called the function directly in-process. This approach avoids
the need to add any special RPC *proxy* objects to the library by instead just
relying on the built-in (async) function calling semantics and protocols of Python.

Depending on the function type ``Portal.run()`` tries to
correctly interface exactly like a local version of the remote
built-in Python function type. Currently async functions, generators,
and regular functions are supported. Inspiration for this API comes
from the way execnet_ does `remote function execution`_ but without
the client code (necessarily) having to worry about the underlying
*channel* API.

This *portal* approach turns out to be paricularly exciting with the
introduction of `asynchronous generators`_ in Python 3.6! It means that
actors can compose nicely in a data processing pipeline.

Say you wanted to spawn two actors which each pulled data feeds from
two different sources (and wanted this work spread across 2 cpus).
You also want to aggregate these feeds, do some processing on them and then
deliver the final result stream to a client (or in this case parent)
actor and print the results to your screen:

.. code:: python

    import time
    import trio
    import tractor


    async def stream_data(seed):
        for i in range(seed):
            yield i
            await trio.sleep(0)  # trigger scheduler


    async def aggregate(seed):
        """Ensure that the two streams we receive match but only stream
        a single set of values to the parent.
        """
        async with tractor.open_nursery() as nursery:
            portals = []
            for i in range(1, 3):
                # fork point
                portal = await nursery.start_actor(
                    name=f'streamer_{i}',
                    rpc_module_paths=[__name__],
                    outlive_main=True,  # daemonize these actors
                )

                portals.append(portal)

            q = trio.Queue(500)

            async def push_to_q(portal):
                async for value in await portal.run(
                    __name__, 'stream_data', seed=seed
                ):
                    # leverage trio's built-in backpressure
                    await q.put(value)

                await q.put(None)
                print(f"FINISHED ITERATING {portal.channel.uid}")

            # spawn 2 trio tasks to collect streams and push to a local queue
            async with trio.open_nursery() as n:
                for portal in portals:
                    n.start_soon(push_to_q, portal)

                unique_vals = set()
                async for value in q:
                    if value not in unique_vals:
                        unique_vals.add(value)
                        # yield upwards to the spawning parent actor
                        yield value

                        if value is None:
                            break

                    assert value in unique_vals

                print("FINISHED ITERATING in aggregator")

            await nursery.cancel()
            print("WAITING on `ActorNursery` to finish")
        print("AGGREGATOR COMPLETE!")


    async def main():
        # a nursery which spawns "actors"
        async with tractor.open_nursery() as nursery:

            seed = int(1e3)
            import time
            pre_start = time.time()

            portal = await nursery.start_actor(
                name='aggregator',
                # executed in the actor's "main task" immediately
                main=partial(aggregate, seed),
            )

            start = time.time()
            # the portal call returns exactly what you'd expect
            # as if the remote "main" function was called locally
            result_stream = []
            async for value in await portal.result():
                result_stream.append(value)

            print(f"STREAM TIME = {time.time() - start}")
            print(f"STREAM + SPAWN TIME = {time.time() - pre_start}")
            assert result_stream == list(range(seed)) + [None]
            return result_stream


    final_stream = tractor.run(main, arbiter_addr=('127.0.0.1', 1616))


Here there's four actors running in separate processes (using all the
cores on you machine). Two are streaming in ``stream_data()``, one is
aggregating values from those two in ``aggregate()`` and shipping the
single stream of unique values up the parent actor (the ``'MainProcess'``
as ``multiprocessing`` calls it) which is running ``main()``. 


There has also been some discussion about adding support for reactive
programming primitives and native support for asyncitertools_ like libs -
so keep an eye out for that!

.. _asynchronous generators: https://www.python.org/dev/peps/pep-0525/
.. _remote function execution: https://codespeak.net/execnet/example/test_info.html#remote-exec-a-function-avoiding-inlined-source-part-i
.. _asyncitertools: https://github.com/vodik/asyncitertools


Cancellation
------------
``tractor`` supports ``trio``'s cancellation_ system verbatim:

.. code:: python

    import trio
    import tractor
    from itertools import repeat


    async def stream_forever():
        for i in repeat("I can see these little future bubble things"):
            yield i
            await trio.sleep(0.01)


    async def main():
        # stream for at most 1 second
        with trio.move_on_after(1) as cancel_scope:
            async with tractor.open_nursery() as n:
                portal = await n.start_actor(
                    f'donny',
                    rpc_module_paths=[__name__],
                    outlive_main=True
                )
                async for letter in await portal.run(__name__, 'stream_forever'):
                    print(letter)

        assert cancel_scope.cancelled_caught
        assert n.cancelled

    tractor.run(main)


Remote error propagation
------------------------
Any task invoked in a remote actor should ship any error(s) back to the calling
actor where it is raised and expected to be dealt with. This way remote actor's
are never cancelled unless explicitly asked or there's a bug in ``tractor`` itself.

.. code:: python

    async def assert_err():
        assert 0

    async def main():
        async with tractor.open_nursery() as n:
            real_actors = []
            for i in range(3):
                real_actors.append(await n.start_actor(
                    f'actor_{i}',
                    rpc_module_paths=[__name__],
                    outlive_main=True
                ))

            # start one actor that will fail immediately
            await n.start_actor('extra', main=assert_err)

        # should error here with a ``RemoteActorError`` containing
        # an ``AssertionError`` and all the other actors have been cancelled

    try:
        # also raises
        tractor.run(main)
    except tractor.RemoteActorError:
        print("Look Maa that actor failed hard, hehhh!")


You'll notice the nursery cancellation conducts a *one-cancels-all*
supervisory strategy `exactly like trio`_. The plan is to add more
`erlang strategies`_ in the near future by allowing nurseries to accept
a ``Supervisor`` type.

.. _exactly like trio: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-semantics
.. _erlang strategies: http://learnyousomeerlang.com/supervisors


Shared task state
-----------------
Although ``tractor`` uses a *shared-nothing* architecture between processes
you can of course share state within an actor.  ``trio`` tasks spawned via
multiple RPC calls to an actor can access global data using the per actor
``statespace`` dictionary:

.. code:: python


        statespace = {'doggy': 10}


        def check_statespace():
            # Remember this runs in a new process so no changes
            # will propagate back to the parent actor
            assert tractor.current_actor().statespace == statespace


        async def main():
            async with tractor.open_nursery() as n:
                await n.start_actor(
                    'checker', main=check_statespace,
                    statespace=statespace
                )


How do actors find each other (a poor man's *service discovery*)?
-----------------------------------------------------------------
Though it will be built out much more in the near future, ``tractor``
currently keeps track of actors by ``(name: str, id: str)`` using a
special actor called the *arbiter*. Currently the *arbiter* must exist
on a host (or it will be created if one can't be found) and keeps a
simple ``dict`` of actor names to sockets for discovery by other actors.
Obviously this can be made more sophisticated (help me with it!) but for
now it does the trick.

To find the arbiter from the current actor use the ``get_arbiter()`` function and to
find an actor's socket address by name use the ``find_actor()`` function:

.. code:: python

    import tractor


    async def main(service_name):

        async with tractor.get_arbiter() as portal:
            print(f"Arbiter is listening on {portal.channel}")

        async with tractor.find_actor(service_name) as sockaddr:
            print(f"my_service is found at {my_service}")


    tractor.run(main, service_name)


The ``name`` value you should pass to ``find_actor()`` is the one you passed as the
*first* argument to either ``tractor.run()`` or ``ActorNursery.start_actor()``.


Using ``Channel`` directly (undocumented)
-----------------------------------------
You can use the ``Channel`` api if necessary by simply defining a
``chan`` and ``cid`` *kwarg* in your async function definition.
``tractor`` will treat such async functions like async generators on
the calling side (for now anyway) such that you can push stream values
a little more granularly if you find *yielding* values to be restrictive.
I am purposely not documenting this feature with code because I'm not yet
sure yet how it should be used correctly. If you'd like more details
please feel free to ask me on the `trio gitter channel`_.


Running actors standalone (without spawning)
--------------------------------------------
You don't have to spawn any actors using ``open_nursery()`` if you just
want to run a single actor that connects to an existing cluster.
All the comms and arbiter registration stuff still works. This can
somtimes turn out being handy when debugging mult-process apps when you
need to hop into a debugger. You just need to pass the existing
*arbiter*'s socket address you'd like to connect to:

.. code:: python

    tractor.run(main, arbiter_addr=('192.168.0.10', 1616))


Enabling logging
----------------
Considering how complicated distributed software can become it helps to know
what exactly it's doing (even at the lowest levels). Luckily ``tractor`` has
tons of logging throughout the core. ``tractor`` isn't opinionated on
how you use this information and users are expected to consume log messages in
whichever way is appropriate for the system at hand. That being said, when hacking
on ``tractor`` there is a prettified console formatted which you can enable to
see what the heck is going on. Just put the following somewhere in your code:

.. code:: python

    from tractor.log import get_console_log
    log = get_console_log('trace')


What the future holds
---------------------
Stuff I'd like to see ``tractor`` do one day:

- erlang-like supervisors_
- native support for zeromq_ as a channel transport
- native `gossip protocol`_ support for service discovery and arbiter election
- a distributed log ledger for tracking cluster behaviour
- a slick multi-process aware debugger much like in celery_
  but with better `pdb++`_ support

If you're interested in tackling any of these please do shout about it on the
`trio gitter channel`_!

.. _supervisors: http://learnyousomeerlang.com/supervisors
.. _zeromq: https://en.wikipedia.org/wiki/ZeroMQ
.. _gossip protocol: https://en.wikipedia.org/wiki/Gossip_protocol
.. _trio gitter channel: https://gitter.im/python-trio/general
.. _celery: http://docs.celeryproject.org/en/latest/userguide/debugging.html
.. _pdb++: https://github.com/antocuni/pdb
