ok now i want you to take a look at the most recent commit adding
a `tpt_bind_addrs` to `open_root_actor()` and extend the existing
tests/discovery/test_multiaddr* and friends to use this new param in
at least one suite with parametrizations over,

- `registry_addrs == tpt_bind_addrs`, as in both inputs are the same.
- `set(registry_addrs) >= set(tpt_bind_addrs)`, as in the registry
  addrs include the bind set.
- `registry_addrs != tpt_bind_addrs`, where the reg set is disjoint from
  the bind set in all possible combos you can imagine.

All of the ^above cases should further be parametrized over,
- the root being the registrar,
- a non-registrar root using our bg `daemon` fixture.

once we have a fairly thorough test suite and have flushed out all
bugs and edge cases we want to design a wrapping API which allows
declaring full tree's of actors tpt endpoints using multiaddrs such
that a `dict[str, list[str]]` of actor-name -> multiaddr can be used
to configure a tree of actors-as-services given such an input
"endpoints-table" can be matched with the number of appropriately
named subactore spawns in a `tractor` user-app.

Here is a small example from piker,

- in piker's root conf.toml we define a `[network]` section which can
  define various actor-service-daemon names set to a maddr
  (multiaddress str).

- each actor whether part of the `pikerd` tree (as a sub) or spawned
  in other non-registrar rooted trees (such as `piker chart`) should
  configurable in terms of its `tractor` tpt bind addresses via
  a simple service lookup table,

  ```toml
  [network]
  pikerd = [
    '/ip4/127.0.0.1/tcp/6116',  # std localhost daemon-actor tree
    '/uds/run/user/1000/piker/pikerd@6116.sock',  # same but serving UDS
  ]
  chart = [
    '/ip4/127.0.0.1/tcp/3333',  # std localhost daemon-actor tree
    '/uds/run/user/1000/piker/chart@3333.sock',
  ]
  ```

We should take whatever common API is needed to support this and
distill it into a
```python
tractor.discovery.parse_endpoints(
) -> dict[
  str,
  list[Address]
  |dict[str, list[Address]]
  # ^recursive case, see below
]:
```

style API which can,

- be re-used easily across dependent projects.
- correctly raise tpt-backend support errors when a maddr specifying
  a unsupport proto is passed.
- be used to handle "tunnelled" maddrs per
  https://github.com/multiformats/py-multiaddr/#tunneling such that
  for any such tunneled maddr-`str`-entry we deliver a data-structure
  which can easily be passed to nested `@acm`s which consecutively
  setup nested net bindspaces for binding the endpoint addrs using
  a combo of our `.ipc.*` machinery and, say for example something like
  https://github.com/svinota/pyroute2, more precisely say for
  managing tunnelled wireguard eps within network-namespaces,
  * https://docs.pyroute2.org/
  * https://docs.pyroute2.org/netns.html

remember to include use of all default `.claude/skills` throughout
this work!
