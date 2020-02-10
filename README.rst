tractor
=======
An async-native "`actor model`_" built on trio_ and multiprocessing_.


|travis|

.. |travis| image:: https://img.shields.io/travis/goodboy/tractor/master.svg
    :target: https://travis-ci.org/goodboy/tractor



``tractor`` is an attempt to bring trionic_ `structured concurrency`_ to
distributed multi-core Python.

``tractor`` lets you spawn ``trio`` *"actors"*: processes which each run
a ``trio`` scheduled task tree (also known as an `async sandwich`_).
*Actors* communicate by exchanging asynchronous messages_ and avoid
sharing any state. This model allows for highly distributed software
architecture which works just as well on multiple cores as it does over
many hosts.

The first step to grok ``tractor`` is to get the basics of ``trio`` down.
A great place to start is the `trio docs`_ and this `blog post`_.

.. _actor model: https://en.wikipedia.org/wiki/Actor_model
.. _trio: https://github.com/python-trio/trio
.. _multiprocessing: https://en.wikipedia.org/wiki/Multiprocessing
.. _trionic: https://trio.readthedocs.io/en/latest/design.html#high-level-design-principles
.. _async sandwich: https://trio.readthedocs.io/en/latest/tutorial.html#async-sandwich
.. _always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _shared nothing architecture: https://en.wikipedia.org/wiki/Shared-nothing_architecture
.. _cancellation: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-and-timeouts
.. _channels: https://en.wikipedia.org/wiki/Channel_(programming)
.. _chaos engineering: http://principlesofchaos.org/
.. _messages: https://en.wikipedia.org/wiki/Message_passing
.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _structured concurrency: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/


Philosophy
----------
``tractor`` aims to be the Python multi-processing framework *you always wanted*.

Its tenets non-comprehensively include:

- strict adherence to the `concept-in-progress`_ of *structured concurrency*
- no spawning of processes *willy-nilly*; causality_ is paramount!
- (remote) errors `always propagate`_ back to the parent supervisor
- verbatim support for ``trio``'s cancellation_ system
- `shared nothing architecture`_
- no use of *proxy* objects or shared references between processes
- an immersive debugging experience
- anti-fragility through `chaos engineering`_


.. warning:: ``tractor`` is in alpha-alpha and is expected to change rapidly!
    Expect nothing to be set in stone. Your ideas about where it should go
    are greatly appreciated!

.. _concept-in-progress: https://trio.discourse.group/t/structured-concurrency-kickoff/55


Install
-------
No PyPi release yet!

::

    pip install git+git://github.com/goodboy/tractor.git


Feel like saying hi?
--------------------
This project is very much coupled to the ongoing development of
``trio`` (i.e. ``tractor`` gets all its ideas from that brilliant
community). If you want to help, have suggestions or just want to
say hi, please feel free to ping me on the `trio gitter channel`_!

.. _trio gitter channel: https://gitter.im/python-trio/general
