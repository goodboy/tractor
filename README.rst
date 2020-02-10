tractor
=======
A `structured concurrent`_, async-native "`actor model`_" built on trio_ and multiprocessing_.

|travis| |docs|

.. toctree::
   :maxdepth: 2
   :caption: Contents:

.. _actor model: https://en.wikipedia.org/wiki/Actor_model
.. _trio: https://github.com/python-trio/trio
.. _multiprocessing: https://en.wikipedia.org/wiki/Multiprocessing
.. _trionic: https://trio.readthedocs.io/en/latest/design.html#high-level-design-principles
.. _async sandwich: https://trio.readthedocs.io/en/latest/tutorial.html#async-sandwich
.. _structured concurrent: https://trio.discourse.group/t/concise-definition-of-structured-concurrency/228


``tractor`` is an attempt to bring trionic_ `structured concurrency`_ to
distributed multi-core Python; it aims to be the Python multi-processing
framework *you always wanted*.

``tractor`` lets you spawn ``trio`` *"actors"*: processes which each run
a ``trio`` scheduled task tree (also known as an `async sandwich`_).
*Actors* communicate by exchanging asynchronous messages_ and avoid
sharing any state. This model allows for highly distributed software
architecture which works just as well on multiple cores as it does over
many hosts.

The first step to grok ``tractor`` is to get the basics of ``trio`` down.
A great place to start is the `trio docs`_ and this `blog post`_.

.. _messages: https://en.wikipedia.org/wiki/Message_passing
.. _trio docs: https://trio.readthedocs.io/en/latest/
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _structured concurrency: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _3 axioms: https://en.wikipedia.org/wiki/Actor_model#Fundamental_concepts
.. _unrequirements: https://en.wikipedia.org/wiki/Actor_model#Direct_communication_and_asynchrony
.. _async generators: https://www.python.org/dev/peps/pep-0525/


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
A `structured concurrent`_, async-native "`actor model`_" built on trio_ and multiprocessing_.


.. |travis| image:: https://img.shields.io/travis/goodboy/tractor/master.svg
    :target: https://travis-ci.org/goodboy/tractor

.. |docs| image:: https://readthedocs.org/projects/tractor/badge/?version=latest
    :target: https://tractor.readthedocs.io/en/latest/?badge=latest
    :alt: Documentation Status
