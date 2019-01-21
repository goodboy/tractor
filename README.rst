tractor
=======
An async-native `actor model`_ built on trio_ and multiprocessing_.


|travis|

.. |travis| image:: https://img.shields.io/travis/tgoodlet/tractor/master.svg
    :target: https://travis-ci.org/tgoodlet/tractor

.. _actor model: https://en.wikipedia.org/wiki/Actor_model
.. _trio: https://github.com/python-trio/trio
.. _multiprocessing: https://docs.python.org/3/library/multiprocessing.html
.. _trionic: https://trio.readthedocs.io/en/latest/design.html#high-level-design-principles
.. _async sandwich: https://trio.readthedocs.io/en/latest/tutorial.html#async-sandwich
.. _always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _shared nothing architecture: https://en.wikipedia.org/wiki/Shared-nothing_architecture
.. _cancellation: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-and-timeouts
.. _channels: https://en.wikipedia.org/wiki/Channel_(programming)
.. _chaos engineering: http://principlesofchaos.org/


``tractor`` is an attempt to bring trionic_ `structured concurrency`_ to distributed multi-core Python.

``tractor`` lets you run and spawn *actors*: processes which each run a ``trio``
scheduler and task tree (also known as an `async sandwich`_).
*Actors* communicate by sending messages_ over channels_ and avoid sharing any local state.
This `actor model`_ allows for highly distributed software architecture which works just as
well on multiple cores as it does over many hosts.
``tractor`` takes much inspiration from pulsar_ and execnet_ but attempts to be much more
focussed on sophistication of the lower level distributed architecture as well as have first
class support for `modern async Python`_.

The first step to grok ``tractor`` is to get the basics of ``trio``
down. A great place to start is the `trio docs`_ and this `blog post`_.

.. _messages: https://en.wikipedia.org/wiki/Message_passing
.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _structured concurrency: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _modern async Python: https://www.python.org/dev/peps/pep-0525/


.. contents::


Philosophy
----------
``tractor``'s tenets non-comprehensively include:

- no spawning of processes *willy-nilly*; causality_ is paramount!
- `shared nothing architecture`_
- remote errors `always propagate`_ back to the caller
- verbatim support for ``trio``'s cancellation_ system
- no use of *proxy* objects to wrap RPC calls
- an immersive debugging experience
- anti-fragility through `chaos engineering`_

.. warning:: ``tractor`` is in alpha-alpha and is expected to change rapidly!
    Expect nothing to be set in stone. Your ideas about where it should go
    are greatly appreciated!

.. _pulsar: http://quantmind.github.io/pulsar/design.html
.. _execnet: https://codespeak.net/execnet/


Install
-------
No PyPi release yet!

::

    pip install git+git://github.com/tgoodlet/tractor.git


Examples
--------


A trynamic first scene
**********************
Let's direct a couple *actors* and have them run their lines for
the hip new film we're shooting:

.. code:: python

    import tractor
    from functools import partial

    _this_module = __name__
    the_line = 'Hi my name is {}'


    async def hi():
        return the_line.format(tractor.current_actor().name)


    async def say_hello(other_actor):
        async with tractor.wait_for_actor(other_actor) as portal:
            return await portal.run(_this_module, 'hi')


    async def main():
        """Main tractor entry point, the "master" process (for now
        acts as the "director").
        """
        async with tractor.open_nursery() as n:
            print("Alright... Action!")

            donny = await n.run_in_actor(
                'donny',
                say_hello,
                other_actor='gretchen',
            )
            gretchen = await n.run_in_actor(
                'gretchen',
                say_hello,
                other_actor='donny',
            )
            print(await gretchen.result())
            print(await donny.result())
            print("CUTTTT CUUTT CUT!!! Donny!! You're supposed to say...")


    tractor.run(main)


We spawn two *actors*, *donny* and *gretchen*.
Each actor starts up and executes their *main task* defined by an
async function, ``say_hello()``.  The function instructs each actor
to find their partner and say hello by calling their partner's
``hi()`` function using something called a *portal*. Each actor
receives a response and relays that back to the parent actor (in
this case our "director" executing ``main()``).


Actor spawning and causality
****************************
``tractor`` tries to take ``trio``'s concept of causal task lifetimes
to multi-process land. Accordingly, ``tractor``'s *actor nursery* behaves
similar to ``trio``'s nursery_. That is, ``tractor.open_nursery()``
opens an ``ActorNursery`` which waits on spawned *actors* to complete
(or error) in the same causal_ way ``trio`` waits on spawned subtasks.
This includes errors from any one actor causing all other actors
spawned by the same nursery to be cancelled_.

To spawn an actor and run a function in it, open a *nursery block*
and use the ``run_in_actor()`` method:

.. code:: python

    import tractor


        def cellar_door():
            return "Dang that's beautiful"


        async def main():
            """The main ``tractor`` routine.
            """
            async with tractor.open_nursery() as n:

                portal = await n.run_in_actor('frank', movie_theatre_question)

            # The ``async with`` will unblock here since the 'frank'
            # actor has completed its main task ``movie_theatre_question()``.

            print(await portal.result())


    tractor.run(main)


What's going on?

- an initial *actor* is started with ``tractor.run()`` and told to execute
  its main task_: ``main()``

- inside ``main()`` an actor is *spawned* using an ``ActorNusery`` and is told
  to run a single function: ``cellar_door()``

- a ``portal`` instance (we'll get to what it is shortly)
  returned from ``nursery.run_in_actor()`` is used to communicate with
  the newly spawned *sub-actor*

- the second actor, *frank*, in a new *process* running a new ``trio`` task_
  then executes ``cellar_door()`` and returns its result over a *channel* back
  to the parent actor

- the parent actor retrieves the subactor's (*frank*) *final result* using ``portal.result()``
  much like you'd expect from a future_.

This ``run_in_actor()`` API should look very familiar to users of
``asyncio``'s `run_in_executor()`_ which uses a ``concurrent.futures`` Executor_.

Since you might also want to spawn long running *worker* or *daemon*
actors, each actor's *lifetime* can be determined based on the spawn
method:

- if the actor is spawned using ``run_in_actor()`` it terminates when
  its *main* task completes (i.e. when the (async) function submitted
  to it *returns*). The ``with tractor.open_nursery()`` exits only once
  all actors' main function/task complete (just like the nursery_ in ``trio``)

- actors can be spawned to *live forever* using the ``start_actor()``
  method and act like an RPC daemon that runs indefinitely (the
  ``with tractor.open_nursery()`` won't exit) until cancelled_

Had we wanted the latter form in our example it would have looked like:

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
            )

            print(await portal.run(__name__, 'movie_theatre_question'))
            # call the subactor a 2nd time
            print(await portal.run(__name__, 'movie_theatre_question'))

            # the async with will block here indefinitely waiting
            # for our actor "frank" to complete, but since it's an
            # "outlive_main" actor it will never end until cancelled
            await portal.cancel_actor()


The ``rpc_module_paths`` `kwarg` above is a list of module path
strings that will be loaded and made accessible for execution in the
remote actor through a call to ``Portal.run()``. For now this is
a simple mechanism to restrict the functionality of the remote
(and possibly daemonized) actor and uses Python's module system to
limit the allowed remote function namespace(s).

``tractor`` is opinionated about the underlying threading model used for
each *actor*. Since Python has a GIL and an actor model by definition
shares no state between actors, it fits naturally to use a multiprocessing_
``Process``. This allows ``tractor`` programs to leverage not only multi-core
hardware but also distribute over many hardware hosts (each *actor* can talk
to all others with ease over standard network protocols).

.. _task: https://trio.readthedocs.io/en/latest/reference-core.html#tasks-let-you-do-multiple-things-at-once
.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
.. _causal: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#causality
.. _cancelled: https://trio.readthedocs.io/en/latest/reference-core.html#child-tasks-and-cancellation
.. _run_in_executor(): https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor
.. _Executor: https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Executor


Async IPC using *portals*
*************************
``tractor`` introduces the concept of a *portal* which is an API
borrowed_ from ``trio``. A portal may seem similar to the idea of
a RPC future_ except a *portal* allows invoking remote *async* functions and
generators and intermittently blocking to receive responses. This allows
for fully async-native IPC between actors.

When you invoke another actor's routines using a *portal* it looks as though
it was called locally in the current actor. So when you see a call to
``await portal.run()`` what you get back is what you'd expect
to if you'd called the function directly in-process. This approach avoids
the need to add any special RPC *proxy* objects to the library by instead just
relying on the built-in (async) function calling semantics and protocols of Python.

Depending on the function type ``Portal.run()`` tries to
correctly interface exactly like a local version of the remote
built-in Python *function type*. Currently async functions, generators,
and regular functions are supported. Inspiration for this API comes
from the way execnet_ does `remote function execution`_ but without
the client code (necessarily) having to worry about the underlying
channels_ system or shipping code over the network.

This *portal* approach turns out to be paricularly exciting with the
introduction of `asynchronous generators`_ in Python 3.6! It means that
actors can compose nicely in a data processing pipeline.

As an example here's an actor that streams for 1 second from a remote async
generator function running in a separate actor:

.. code:: python

    from itertools import repeat
    import trio
    import tractor


    async def stream_forever():
        for i in repeat("I can see these little future bubble things"):
            # each yielded value is sent over the ``Channel`` to the
            # parent actor
            yield i
            await trio.sleep(0.01)


    async def main():
        # stream for at most 1 seconds
        with trio.move_on_after(1) as cancel_scope:
            async with tractor.open_nursery() as n:
                portal = await n.start_actor(
                    f'donny',
                    rpc_module_paths=[__name__],
                )

                # this async for loop streams values from the above
                # async generator running in a separate process
                async for letter in await portal.run(__name__, 'stream_forever'):
                    print(letter)

        # we support trio's cancellation system
        assert cancel_scope.cancelled_caught
        assert n.cancelled


    tractor.run(main)



A full fledged streaming service
********************************
Alright, let's get fancy.

Say you wanted to spawn two actors which each pull data feeds from
two different sources (and wanted this work spread across 2 cpus).
You also want to aggregate these feeds, do some processing on them and then
deliver the final result stream to a client (or in this case parent) actor
and print the results to your screen:

.. code:: python

    import time
    import trio
    import tractor


    # this is the first 2 actors, streamer_1 and streamer_2
    async def stream_data(seed):
        for i in range(seed):
            yield i
            await trio.sleep(0)  # trigger scheduler


    # this is the third actor; the aggregator
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


    # this is the main actor and *arbiter*
    async def main():
        # a nursery which spawns "actors"
        async with tractor.open_nursery() as nursery:

            seed = int(1e3)
            import time
            pre_start = time.time()

            portal = await nursery.run_in_actor(
                'aggregator',
                aggregate,
                seed=seed,
            )

            start = time.time()
            # the portal call returns exactly what you'd expect
            # as if the remote "aggregate" function was called locally
            result_stream = []
            async for value in await portal.result():
                result_stream.append(value)

            print(f"STREAM TIME = {time.time() - start}")
            print(f"STREAM + SPAWN TIME = {time.time() - pre_start}")
            assert result_stream == list(range(seed)) + [None]
            return result_stream


    final_stream = tractor.run(main, arbiter_addr=('127.0.0.1', 1616))


Here there's four actors running in separate processes (using all the
cores on you machine). Two are streaming by *yielding* values from the
``stream_data()`` async generator, one is aggregating values from
those two in ``aggregate()`` (also an async generator) and shipping the
single stream of unique values up the parent actor (the ``'MainProcess'``
as ``multiprocessing`` calls it) which is running ``main()``. 

.. _future: https://en.wikipedia.org/wiki/Futures_and_promises
.. _borrowed:
    https://trio.readthedocs.io/en/latest/reference-core.html#getting-back-into-the-trio-thread-from-another-thread
.. _asynchronous generators: https://www.python.org/dev/peps/pep-0525/
.. _remote function execution: https://codespeak.net/execnet/example/test_info.html#remote-exec-a-function-avoiding-inlined-source-part-i
.. _asyncitertools: https://github.com/vodik/asyncitertools


Cancellation
************
``tractor`` supports ``trio``'s cancellation_ system verbatim.
Cancelling a nursery block cancels all actors spawned by it.
Eventually ``tractor`` plans to support different `supervision strategies`_ like ``erlang``.

.. _supervision strategies: http://erlang.org/doc/man/supervisor.html#sup_flags


Remote error propagation
************************
Any task invoked in a remote actor should ship any error(s) back to the calling
actor where it is raised and expected to be dealt with. This way remote actors
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
                ))

            # start one actor that will fail immediately
            await n.run_in_actor('extra', assert_err)

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


Actor local variables
*********************
Although ``tractor`` uses a *shared-nothing* architecture between processes
you can of course share state between tasks running *within* an actor.
``trio`` tasks spawned via multiple RPC calls to an actor can access global
state using the per actor ``statespace`` dictionary:

.. code:: python


        statespace = {'doggy': 10}


        def check_statespace():
            # Remember this runs in a new process so no changes
            # will propagate back to the parent actor
            assert tractor.current_actor().statespace == statespace


        async def main():
            async with tractor.open_nursery() as n:
                await n.run_in_actor(
                    'checker',
                    check_statespace,
                    statespace=statespace
                )


Of course you don't have to use the ``statespace`` variable (it's mostly
a convenience for passing simple data to newly spawned actors); building
out a state sharing system per-actor is totally up to you.


How do actors find each other (a poor man's *service discovery*)?
*****************************************************************
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


Streaming using channels and contexts
*************************************
``Channel`` is the API which wraps an underlying *transport* and *interchange*
format to enable *inter-actor-communication*. In its present state ``tractor``
uses TCP and msgpack_.

If you aren't fond of having to write an async generator to stream data
between actors (or need something more flexible) you can instead use a
``Context``. A context wraps an actor-local spawned task and a ``Channel``
so that tasks executing across multiple processes can stream data
to one another using a low level, request oriented API.

As an example if you wanted to create a streaming server without writing
an async generator that *yields* values you instead define an async
function:

.. code:: python

   async def streamer(ctx, rate=2):
      """A simple web response streaming server.
      """
      while True:
         val = await web_request('http://data.feed.com')

         # this is the same as ``yield`` in the async gen case
         await ctx.send_yield(val)

         await trio.sleep(1 / rate)


All that's required is declaring a ``ctx`` argument name somewhere in
your function signature and ``tractor`` will treat the async function
like an async generator - as a streaming function from the client side.
This turns out to be handy particularly if you have
multiple tasks streaming responses concurrently:

.. code:: python

   async def streamer(ctx, url, rate=2):
      """A simple web response streaming server.
      """
      while True:
         val = await web_request(url)

         # this is the same as ``yield`` in the async gen case
         await ctx.send_yield(val)

         await trio.sleep(1 / rate)


   async def stream_multiple_sources(ctx, sources):
      async with trio.open_nursery() as n:
         for url in sources:
            n.start_soon(streamer, ctx, url)


The context notion comes from the context_ in nanomsg_.


Running actors standalone
*************************
You don't have to spawn any actors using ``open_nursery()`` if you just
want to run a single actor that connects to an existing cluster.
All the comms and arbiter registration stuff still works. This can
somtimes turn out being handy when debugging mult-process apps when you
need to hop into a debugger. You just need to pass the existing
*arbiter*'s socket address you'd like to connect to:

.. code:: python

    tractor.run(main, arbiter_addr=('192.168.0.10', 1616))


Enabling logging
****************
Considering how complicated distributed software can become it helps to know
what exactly it's doing (even at the lowest levels). Luckily ``tractor`` has
tons of logging throughout the core. ``tractor`` isn't opinionated on
how you use this information and users are expected to consume log messages in
whichever way is appropriate for the system at hand. That being said, when hacking
on ``tractor`` there is a prettified console formatter which you can enable to
see what the heck is going on. Just put the following somewhere in your code:

.. code:: python

    from tractor.log import get_console_log
    log = get_console_log('trace')


What the future holds
---------------------
Stuff I'd like to see ``tractor`` do real soon:

- erlang-like supervisors_
- native support for `nanomsg`_ as a channel transport
- native `gossip protocol`_ support for service discovery and arbiter election
- a distributed log ledger for tracking cluster behaviour
- a slick multi-process aware debugger much like in celery_
  but with better `pdb++`_ support
- an extensive `chaos engineering`_ test suite
- support for reactive programming primitives and native support for asyncitertools_ like libs


Feel like saying hi?
--------------------
This project is very much coupled to the ongoing development of
``trio`` (i.e. ``tractor`` gets all its ideas from that brilliant
community). If you want to help, have suggestions or just want to
say hi, please feel free to ping me on the `trio gitter channel`_!


.. _supervisors: https://github.com/tgoodlet/tractor/issues/22
.. _nanomsg: https://nanomsg.github.io/nng/index.html
.. _context: https://nanomsg.github.io/nng/man/tip/nng_ctx.5
.. _gossip protocol: https://en.wikipedia.org/wiki/Gossip_protocol
.. _trio gitter channel: https://gitter.im/python-trio/general
.. _celery: http://docs.celeryproject.org/en/latest/userguide/debugging.html
.. _pdb++: https://github.com/antocuni/pdb
.. _msgpack: https://en.wikipedia.org/wiki/MessagePack
