Project
=======
Everything meta: where the project's been (the
:doc:`changelog`), where it's going (the roadmap below),
how to hack on it (:doc:`dev-tips` and
:doc:`/guide/testing`) and where to find the humans.

.. toctree::
   :maxdepth: 1

   changelog
   dev-tips

What the future holds
---------------------
Help us push toward the future of distributed Python!
Planned (or dreamed of) work non-comprehensively
includes,

- Erlang-style supervisors via composed context
  managers (see `#22`_),
- typed capability-based (dialog) protocols, ie.
  evolving our `msg-spec`_ system into per-dialog
  contracts (see `#196`_ with draft work in `#311`_),
- a higher level "service manager" API for daemon
  lifetime mgmt over actor trees (in the works on an
  experimental branch as ``tractor.hilevel``),
- richer `discovery`_ via gossip and/or
  `rendezvous protocol`_ approaches (today's registrar
  is intentionally naive),
- more IPC transports: the current ``tcp`` | ``uds``
  pair wants friends (QUIC, shm-ring-buffers, RUDP,
  wireguard tunnels),
- an extensive `chaos engineering`_ test suite,
- a respawn-from-REPL system for crashed (sub-)actors.

Feel like saying hi?
--------------------
This project is very much coupled to the ongoing
development of ``trio`` (i.e. ``tractor`` gets most of
its ideas from that brilliant community). If you want
to help, have suggestions or just want to say hi,
please feel free to reach us in our `matrix channel`_.
If matrix seems too hip, we're also mostly all in the
`trio gitter channel`_!

Contributions of all kinds welcome: docs, examples,
bug reports, new transports, supervisor strategies,
philosophical debates about what an "actor model"
really is B)

.. _#22: https://github.com/goodboy/tractor/issues/22
.. _#196: https://github.com/goodboy/tractor/issues/196
.. _#311: https://github.com/goodboy/tractor/pull/311
.. _msg-spec: https://jcristharif.com/msgspec/
.. _discovery: https://zguide.zeromq.org/docs/chapter8/#Discovery
.. _rendezvous protocol: https://en.wikipedia.org/wiki/Rendezvous_protocol
.. _chaos engineering: https://principlesofchaos.org/
.. _matrix channel: https://matrix.to/#/!tractor:matrix.org
.. _trio gitter channel: https://gitter.im/python-trio/general
