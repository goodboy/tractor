# NLnet NGI Zero Commons Fund - `tractor` Grant Application Draft

> **Call:** NGI Zero Commons Fund (12th call)
> **Deadline:** April 1, 2026 at 12:00 CEST
> **Budget range:** EUR 5,000 - 50,000
> **Status:** FIRST DRAFT - needs review and refinement

---

## Contact Information

> **Your name**

Tyler Goodlet

---

> **Email address**

goodboy_foss@protonmail.com

---

> **Phone number**

`[TODO: fill in]`

---

> **Organisation**

Independent but with a surrounding (dependent project) community, https://pikers.dev

---

> **Country**

Global but with affiliated community meetups often in CDMX, Mexico.

---

## General Project Information

> **Proposal name**

tractor: distributed structured concurrency

---

> **Website / wiki**

https://github.com/goodboy/tractor

---

> **Abstract: Can you explain the whole project and its expected
> outcome(s). Be short and to the point; focus on what and how, not
> why.**

`tractor` is a distributed structured concurrency (SC)
multi-processing runtime for Python built on (and effectively
extending) the marvel that is `trio` for distributed computing. It
provides a novel approach to what some might define as an
"actor-model" by applying SC throughout a (distributed) Python
process tree, effectively implementing a "Single program system"
(SPS) akin to the EVM.

`tractor` "actors" interact rigorously via a "supervision-control
protocol" (SCP) enforced via a "typed IPC messaging spec" allowing
for the implementation of and "end-to-end-SC-adhering (optionally
distributed) multi-process embedded `trio.Task` tree". 

The primary outcome of this design is rigorous adherence to SC within
parallel compute and distributed systems more generally.

In a one liner, the primary desired outcome is that "no concurrency
primitive child (thread) can outlive or "zombie" its parent
(supervisor)" despite separation of execution-scope in the memory
domain.

The surprising benefits of this design include,
- support for infinitely nestable "actor nurseries" and thus SC
  supervised process trees.
- bi-directional streaming with extremely reliable setup/teardown and
  error propagation across complex architectures.
- modular IPC with pluggable, composed transports (TCP, UDS, QUIC)
  and serialized interchange formats (currently via `msgspec`).
- a deterministic UX for multi-process debugging/tracing (currently
  via a builtint `pdbp` integration).
- cross "async framework" (`asyncio`, Qt) embedded event-loop hosting
  thanks to `trio`'s "guest mode" which provides for `asyncio` code
  controlled by `trio`-supervised actors.

This grant funds the push from alpha/beta toward a stable 1.0
release, targeting 6 major milestones:

1. **Typed messaging protocols** - formalizing capability-based dialog
   protocols using `msgspec.Struct` types so inter-actor contracts are
   statically verifiable (issues #36, #196, #311).

2. **Real documentation** - actually providing a set of tutorials,
   accompanying diagrams (ideally auto-generated with D2) and general
   usage guides outside our lone readme + examples.

3. **Erlang-style supervision APIs** - composable supervisor
   strategies (one-for-one, one-for-all, rest-for-one) via context
   manager composition, enabling robust fault recovery without manual
   restart logic (issue #22).

4. **Next-gen discovery system + addressing** - implementation of
   a non-naive builtin discovery sub-system with support for
   "multi-addresses" as an alternative (and arguably superior)
   mechanism for service discovery on the internet.

5. **Encrypted transport backend(s)** - either via plain ol
   TLS-equivalents or via composition with tunnel protocols such
   as wireguard, SSH or QUIC.

6. **Supporting super high-perf ng IPC transports** - namely native
   support for `eventfd` + shared-mem channels for local-host and
   TIPC for multi-host setups.

Secondary outcomes include improved a stabilized public API surface,
possible inter-language integration (once the SCP pattern is better
defined) and obviously a (at least) beta-quality release on PyPI.

---

> **Have you been involved with projects or organisations relevant to
> this project before? And if so, can you tell us a bit about that?**

I am the creator and primary maintainer of `tractor`, the project's
inception came out of a grander project `piker` a ground-up FOSS
computational trading stack. The project originally grew from
practical needs building real-time financial data processing systems
requiring extremely robust multi-core, multiple language (including
Python) execution environments with grokable failure semantics.

I am a long time active contributor and participant in the `trio` and
surrounding structured concurrency community and have contributed to
SC shaping discussions around semantics and interfaces generally in
Python land. `tractor` itself maintains a very lightly trafficked
Matrix channel but myself and fellow contributors engage more
frequently with the broader async ecosystem and our surrounding
dependee projects.

Prior to `tractor`, I worked extensively with Python's
`multiprocessing`, `asyncio` and dependent projects, various
actor-like-systems/protocols in other langs (elixir/erlang, akka,
0mq), all of which informed (and which continue to) the design
decisions - particularly the rejection of proxy objects, mailbox
abstractions, shared-memory (vs. msg-passing), lack of required
supervision primitives (and arguably the required IPC augmentation
for such semantics) in favor of this burgeoning concept of "SC-native
process supervision".

---

## Requested Support

> **Requested Amount (in euros)**

EUR 50,000

---

> **Explain what the requested budget will be used for. Provide a
> breakdown of main tasks and effort with explicit rates. Full budget
> may be attached.**

All work is performed by the sole core maintainer and extremely
trusted/vetted core contributors at a rate of EUR 50/hr. The budget
breaks down across the 6 primary work packages:

**WP1: Typed messaging and dialog protocols (EUR 18,000 / ~360 hrs)**
- Define `msgspec.Struct`-based message schemas for all IPC
  primitives (RPC calls, streaming, context dialogs)
- Implement compile-time and runtime type validation at IPC boundaries
- Build capability-based dialog protocol negotiation between actors
- Write comprehensive test coverage and migration guide
- Refs: #36, #196, #311

**WP2: Erlang-style supervision strategies (EUR 14,000 / ~280 hrs)**
- Design and implement composable supervisor context managers
  supporting one-for-one, one-for-all, and rest-for-one restart
  strategies
- Integrate with existing `ActorNursery` and error propagation
  machinery
- Add configurable restart limits, backoff policies, and supervision
  tree introspection
- Test under chaos-engineering fault injection scenarios
- Ref: #22

**WP3: Transport security and stabilization (EUR 10,000 / ~200 hrs)**
- Add TLS encryption for TCP-based inter-host actor links
- Harden the UDS (Unix Domain Socket) transport path
- Stabilize the public API surface and deprecate internal interfaces
- Audit and fix edge cases in remote exception relay and cancellation

**WP4: Documentation and release (EUR 8,000 / ~160 hrs)**
- Write Sphinx-based user guide covering core APIs, patterns, and
  deployment
- Create tutorial series (single-host, multi-host, asyncio
  integration)
- Publish beta release to PyPI with changelog and migration notes
- Update examples to demonstrate new typed protocols and supervisors

---

> **Does the project have other funding sources, both past and
> present? (if so, please describe)**

No. `tractor` has been entirely self-funded namely through downstream
dependent projects which themselves are similar WIPs in the FOSS
computational (financial) trading space. It is developed as
a completely volunteer and "as needed" open-source effort. There are
no corporate sponsors, institutional backers, or prior grants; the
only "funding" has come via piece-wise, proxy contract engineering
work on said dependent projects. This would be the project's first
official external funding.

---

> **Compare your own project with existing or historical efforts.**

**vs. Python `multiprocessing` / `concurrent.futures`**: These stdlib
modules provide process pools but no structured lifecycle management.
A crashed worker can leave the pool in undefined state; there is no
supervision tree, no cross-process cancellation propagation, and no
supervised streaming IPC. `tractor` enforces that every child process
is bound to its parent's lifetime via SC nurseries.

**vs. Celery / Dramatiq / other task queues**: Task queues require
external brokers (Redis, RabbitMQ), operate on a fire-and-forget
model, and provide no structured error propagation. `tractor`
eliminates the broker dependency, provides bidirectional streaming,
and guarantees that remote errors propagate to the calling scope. It
can be thought of as the 0mq of runtimes to the AMQP, it take
sophistication to a lower level allowing for easily building any
distributed architecture you can imagine.

**vs. Ray / Dask**: These target data-parallel and ML workloads with
cluster schedulers and also contain no adherence to SC. They use
proxy objects and shared-memory abstractions in ways that break SC
guarantees. `tractor` targets general distributed programming and can
easily accomplish similar feats with arguably with less code and
surrounding tooling.

**vs. Erlang/OTP (BEAM)**: `tractor` is directly inspired by OTP's
supervision trees but implements them in Python using `trio`'s
rigorous SC primitives (nurseries/task-groups, cancel-scopes,
thread-isolation) rather than a custom VM. We aim to bring OTP-grade
reliability to the Python ecosystem without requiring developers to
leave their existing language and toolchain and further use the same
primitives to eventually do the same in other langs.

**vs. `trio-parallel`**: `trio-parallel` solves a narrower problem:
running sync functions in worker processes. `tractor` provides the
full actor runtime - nested process trees, bidirectional streaming,
remote debugging, and distributed deployment, etc.

**vs. Actor frameworks (Pykka, Thespian)**: These implement actor
patterns atop threads or `asyncio` but do not enforce structured
concurrency. Actors can outlive their creators, errors can be
silently dropped, and there is no systematic cancellation. `tractor`
is SC from the ground up.

---

> **What significant technical challenges do you expect to solve
> during the project?**

1. **Type-safe IPC without performance regression**: Introducing
   `msgspec.Struct`-based typed message validation at every IPC
   boundary must not degrade throughput. The challenge is designing a
   schema layer that enables zero-copy deserialization while providing
   meaningful compile-time and runtime type checking.

2. **Composable supervision under SC constraints**: Erlang's OTP
   supervisors rely on process linking and message-based monitoring.
   Translating these patterns into `trio`'s task-nursery and
   cancellation-scope model - where parent scopes *must* outlive
   children - requires novel composition of context managers and
   careful interaction with Python's exception groups.

3. **TLS in a dynamic actor topology**: Actors spawn and connect
   dynamically. Implementing mutual TLS authentication without a
   centralized certificate authority, while supporting both long-lived
   daemons and ephemeral workers, requires a lightweight trust model
   compatible with ad-hoc process tree formation.

4. **API stabilization without breaking SC invariants**: Moving from
   alpha to beta means committing to a public API surface. The
   challenge is identifying which internal interfaces can be safely
   frozen vs. which need further iteration, while ensuring that any
   API changes preserve the runtime's SC guarantees.

5. **Cross-platform debugging under encrypted transports**: The
   multi-process `pdbp` debugger currently relies on unencrypted IPC
   for TTY lock coordination. Adding naive TLS must not break the
   debugging experience, requiring careful layering of debug-control
   messages and/or requiring embedded tunnelling.

---

> **Describe the ecosystem of the project, and how you will engage
> with relevant actors and promote outcomes.**

`tractor` operates within the Python async/concurrency ecosystem,
primarily adjacent to the `trio` community:

**Upstream dependencies:**
- `trio`/`anyio` (structured concurrency runtimes) - we track
  upstream development closely and participate in design discussions.
- `msgspec` (high-performance serialization) - our typed messaging
  work will provide real-world feedback to the `msgspec` maintainer.
- `pdbp` (debugger REPL) - we actively collaborate on fixes.

**User communities:**
- Python developers building distributed systems who need stronger
  guarantees than `multiprocessing` or alts provide.
- The `trio` user community seeking SC + parallelism.
- Scientific computing users wanting robust process supervision
  without Dask/Ray's deployment complexity.
- FOSS computational trader via the aforementioned `piker`.

**Engagement plan:**
- Maintain active Matrix channel (`#tractor:matrix.org`) for user
  support and contributor onboarding.
- Publish milestone blog posts on the `trio` Discourse forum.
- Present at Python (and distributed-compute) conferences (PyCon,
  EuroPython) if accepted.
- Contribute learnings about distributed SC back to the `trio`
  project's design discussions.
- Engage with the broader SC community (Kotlin coroutines, Swift
  structured concurrency, Java Loom) to cross-pollinate ideas.
- All code, documentation, and design documents released under
  AGPL-3.0-or-later on GitHub, with mirrors on sourcehut and
  self-hosted Gitea.

---

## Thematic Call Selection

> **Call topic**

NGI Zero Commons Fund

---

## Generative AI Disclosure

> **Use of generative AI**

I have used generative AI in writing this proposal.

---

> **Which model did you use? What did you use it for?**

Model: Claude Opus 4.6 (Anthropic), via the `claude-code` CLI tool.

Usage: The AI was used to generate a first draft of all form field
responses based on the project's existing documentation (README,
pyproject.toml, git history, issue tracker). The NLnet submission form
was fetched and parsed to identify all required fields and their
guidance text. All draft responses were then reviewed, edited, and
refined by the project maintainer before submission.

The unedited AI output and prompts are available in the project
repository under `funding/`.

`[TODO: attach prompt log before submission]`

---

## Notes for Review

### Before submitting, address these TODOs:

- [ ] Fill in phone number
- [ ] Fill in organisation (or confirm "Independent")
- [ ] Fill in country
- [ ] Review and refine the **Abstract** - is the scope right? Too
      ambitious? Trim or expand milestones as needed.
- [ ] Validate the **budget breakdown** - are the hourly rate and
      hour estimates reasonable? Adjust WP allocations.
- [ ] Review **requested amount** - EUR 50,000 is the max; consider
      whether a smaller, more focused ask is strategically better.
- [ ] Decide which **issues/PRs** to highlight most prominently
- [ ] Consider whether to attach a **roadmap PDF** with more detail
- [ ] Review the **comparison section** - add/remove competitors as
      appropriate
- [ ] Refine the **ecosystem** section with specific community
      contacts or partnerships
- [ ] Save prompt logs for AI disclosure attachment
- [ ] Proofread everything for accuracy and tone
- [ ] **Submit before April 1, 2026 12:00 CEST**
