This is your first big boi, "from GH issue" design, plan and
implement task.

We need to try and add sub-interpreter (aka subint) support per the
issue,

https://github.com/goodboy/tractor/issues/379

Part of this work should include,

- modularizing and thus better organizing the `.spawn.*` subpkg by
  breaking up various backends currently in `spawn._spawn` into
  separate submods where it makes sense.

- add a new `._subint` backend which tries to keep as much of the
  inter-process-isolation machinery in use as possible but with plans
  to optimize for localhost only benefits as offered by python's
  subints where possible.

  * utilizing localhost-only tpts like UDS, shm-buffers for
    performant IPC between subactors but also leveraging the benefits from
    the traditional OS subprocs mem/storage-domain isolation, linux
    namespaces where possible and as available/permitted by whatever
    is happening under the hood with how cpython implements subints.

  * default configuration should encourage state isolation as with
    subprocs, but explicit public escape hatches to enable rigorously
    managed shm channels for high performance apps.

- all tests should be (able to be) parameterized to use the new
  `subints` backend and enabled by flag in the harness using the
  existing `pytest --spawn-backend <spawn-backend>` support offered in
  the `open_root_actor()` and `.testing._pytest` harness override
  fixture.
