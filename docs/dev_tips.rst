Hot tips for ``tractor`` hackers
================================

This is a WIP guide for newcomers to the project mostly to do with
dev, testing, CI and release gotchas, reminders and best practises.

``tractor`` is a fairly novel project compared to most since it is
effectively a new way of doing distributed computing in Python and is
much closer to working with an "application level runtime" (like erlang
OTP or scala's akka project) then it is a traditional Python library.
As such, having an arsenal of tools and recipes for figuring out the
right way to debug problems when they do arise is somewhat of
a necessity.


Making a Release
----------------
We currently do nothing special here except the traditional
PyPa release recipe as in `documented by twine`_. I personally
create sub-dirs within the generated `dist/` with an explicit
release name such as `alpha3/` when there's been a sequence of
releases I've made, but it really is up to you how you like to
organize generated sdists locally.

The resulting build cmds are approximately:

.. code:: bash

    python setup.py sdist -d ./dist/XXX.X/

    twine upload -r testpypi dist/XXX.X/*

    twine upload dist/XXX.X/*



.. _documented by twine: https://twine.readthedocs.io/en/latest/#using-twine


Debugging and monitoring actor trees
------------------------------------
TODO: but there are tips in the readme for some terminal commands
which can be used to see the process trees easily on Linux.


Using the log system to trace `trio` task flow
----------------------------------------------
TODO: the logging system is meant to be oriented around
stack "layers" of the runtime such that you can track
"logical abstraction layers" in the code such as errors, cancellation,
IPC and streaming, and the low level transport and wire protocols.
