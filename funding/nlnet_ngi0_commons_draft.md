# NLnet NGI Zero Commons Fund - `tractor` Grant Application Draft

> **Call:** NGI Zero Commons Fund (12th call)
> **Deadline:** April 1, 2026 at 12:00 CEST
> **Budget range:** EUR 5,000 - 50,000
> **Status:** FIRST DRAFT - needs review and refinement

---

## Contact Information

> **Your name**

Tyler Goodlet

> **Email address**

goodboy_foss@protonmail.com

> **Phone number**

`[TODO: fill in]`

> **Organisation**

`[TODO: fill in or "Independent / self-employed"]`

> **Country**

`[TODO: fill in]`

---

## General Project Information

> **Proposal name**

tractor: structured concurrent distributed Python

> **Website / wiki**

https://github.com/goodboy/tractor

> **Abstract: Can you explain the whole project and its expected
> outcome(s). Be short and to the point; focus on what and how, not
> why.**

`tractor` is a distributed structured concurrency (SC) runtime for
Python built on `trio`. It provides an actor-model-based
multi-processing framework where independent Python processes
communicate via typed IPC while maintaining end-to-end SC guarantees
across the entire process tree - no child can outlive or "zombie" its
parent.

The runtime currently offers: infinitely nestable actor nurseries,
bi-directional streaming with reliable teardown, modular IPC with
pluggable transports (TCP, UDS) and serialization (`msgspec`), native
multi-process debugging via `pdbp`, and an "infected asyncio" mode
that wraps `asyncio` code in `trio`-supervised actors.

This grant funds the push from alpha toward a stable 1.0 release,
targeting three milestones:

1. **Typed messaging protocols** - implement capability-based dialog
   protocols using `msgspec.Struct` types so inter-actor contracts are
   statically verifiable (issues #36, #196, #311).

2. **Erlang-style supervision** - composable supervisor strategies
   (one-for-one, one-for-all, rest-for-one) via context manager
   composition, enabling robust fault recovery without manual restart
   logic (issue #22).

3. **Transport hardening and encryption** - TLS support for
   cross-host actor links, completing the path from single-machine
   parallelism to secure distributed deployment.

Secondary outcomes include improved documentation, a stabilized public
API surface, and a beta-quality release on PyPI.

> **Have you been involved with projects or organisations relevant to
> this project before? And if so, can you tell us a bit about that?**

I am the creator and primary maintainer of `tractor`, having authored
over 4,500 of its ~4,700 commits since the project's inception. The
project grew from practical needs while building real-time financial
data systems requiring robust multi-process Python with predictable
failure semantics.

I am an active participant in the `trio` structured concurrency
community and have contributed to discussions shaping SC semantics in
Python. The project maintains an active Matrix channel and engages
with the broader Python async ecosystem.

Prior to `tractor`, I worked extensively with Python's
`multiprocessing`, `asyncio`, and various actor frameworks (Thespian,
`dramatiq`), which informed the design decisions - particularly the
rejection of proxy objects, mailbox abstractions, and shared-memory
models in favor of SC-native process supervision.

---

## Requested Support

> **Requested Amount (in euros)**

EUR 50,000

> **Explain what the requested budget will be used for. Provide a
> breakdown of main tasks and effort with explicit rates. Full budget
> may be attached.**

All work is performed by the sole core maintainer at a rate of EUR
50/hr. The budget breaks down across three primary work packages:

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

> **Does the project have other funding sources, both past and
> present? (if so, please describe)**

No. `tractor` has been entirely self-funded and developed as a
volunteer open-source effort. There are no corporate sponsors,
institutional backers, or prior grants. This would be the project's
first external funding.

> **Compare your own project with existing or historical efforts.**

**vs. Python `multiprocessing` / `concurrent.futures`**: These
stdlib modules provide process pools but no structured lifecycle
management. A crashed worker can leave the pool in undefined state;
there is no supervision tree, no cross-process cancellation
propagation, and no streaming IPC. `tractor` enforces that every child
process is bound to its parent's lifetime via SC nurseries.

**vs. Celery / Dramatiq / other task queues**: Task queues require
external brokers (Redis, RabbitMQ), operate on a fire-and-forget
model, and provide no structured error propagation. `tractor`
eliminates the broker dependency, provides bidirectional streaming,
and guarantees that remote errors propagate to the calling scope.

**vs. Ray / Dask**: These target data-parallel and ML workloads with
cluster schedulers. They use proxy objects and shared-memory
abstractions that break SC guarantees. `tractor` targets general
distributed programming with strict process isolation and predictable
failure modes, not batch data processing.

**vs. Erlang/OTP (BEAM)**: `tractor` is directly inspired by OTP's
supervision trees but implements them in Python using `trio`'s SC
primitives and context managers rather than a custom VM. We aim to
bring OTP-grade reliability to the Python ecosystem without requiring
developers to leave their existing language and toolchain.

**vs. `trio-parallel`**: `trio-parallel` solves a narrower problem:
running sync functions in worker processes. `tractor` provides the
full actor runtime - nested process trees, bidirectional streaming,
remote debugging, and distributed deployment.

**vs. Actor frameworks (Pykka, Thespian)**: These implement actor
patterns atop threads or `asyncio` but do not enforce structured
concurrency. Actors can outlive their creators, errors can be silently
dropped, and there is no systematic cancellation. `tractor` is SC
from the ground up.

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
   for TTY lock coordination. Adding TLS must not break the debugging
   experience, requiring careful layering of debug-control messages.

> **Describe the ecosystem of the project, and how you will engage
> with relevant actors and promote outcomes.**

`tractor` operates within the Python async/concurrency ecosystem,
primarily adjacent to the `trio` community:

**Upstream dependencies:**
- `trio` (structured concurrency runtime) - we track upstream
  development closely and participate in design discussions
- `msgspec` (high-performance serialization) - our typed messaging
  work will provide real-world feedback to the `msgspec` maintainer
- `pdbp` (debugger REPL) - we actively collaborate on fixes

**User communities:**
- Python developers building distributed systems who need stronger
  guarantees than `multiprocessing` provides
- The `trio` user community seeking multi-process parallelism
- Scientific computing users wanting robust process supervision
  without Dask/Ray's complexity

**Engagement plan:**
- Maintain active Matrix channel (`#tractor:matrix.org`) for user
  support and contributor onboarding
- Publish milestone blog posts on the `trio` Discourse forum
- Present at Python conferences (PyCon, EuroPython) if accepted
- Contribute learnings about distributed SC back to the `trio`
  project's design discussions
- Engage with the broader SC community (Kotlin coroutines, Swift
  structured concurrency, Java Loom) to cross-pollinate ideas
- All code, documentation, and design documents released under
  AGPL-3.0-or-later on GitHub, with mirrors on sourcehut and
  self-hosted Gitea

---

## Thematic Call Selection

> **Call topic**

NGI Zero Commons Fund

---

## Generative AI Disclosure

> **Use of generative AI**

I have used generative AI in writing this proposal.

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
