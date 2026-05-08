# `fork()` in a multi-threaded program — execution-side vs. memory-side of the same coin

A reference doc for readers who've encountered one of two
opposite-sounding framings of POSIX `fork()` semantics in a
multi-threaded program and are confused by the other.

This is a sibling to
`subint_fork_blocked_by_cpython_post_fork_issue.md` — that
doc covers a CPython-level refusal of fork-from-subint;
this one covers the more general POSIX layer, since
tractor's main-thread forkserver design rests on it.

## TL;DR

POSIX `fork()` only preserves the *calling* thread as a
runnable thread in the child — every other thread in the
parent simply never executes another instruction in the
child. trio's docs call this "leaked"; tractor's
`_main_thread_forkserver.py` docstring calls it "gone".
Both are correct: "gone" is the *execution* side (no
scheduler entry, no instructions retired), "leaked" is the
*memory* side (the dead threads' stacks and per-thread
heap structures still ride into the child's address space
as orphaned COW pages with no owner and no cleanup hook).
Same POSIX reality, two halves of the same coin.

## The two framings

[python-trio/trio#1614][trio-1614] (the canonical "trio +
fork" hazards thread) puts it this way:

> If you use `fork()` in a process with multiple threads,
> all the other thread stacks are just leaked: there's
> nothing else you can reasonably do with them.

`tractor.spawn._main_thread_forkserver`'s module docstring
(specifically the "What survives the fork? — POSIX
semantics" section) puts it this way:

> POSIX `fork()` only preserves the *calling* thread as a
> runnable thread in the child. Every other thread in the
> parent — trio's runner thread, any `to_thread` cache
> threads, anything else — never executes another
> instruction post-fork.

A reader bouncing between the two can be forgiven for
asking: well, *which* is it — leaked or gone?

The answer is "yes". They're describing the same POSIX
behavior from two different angles:

- trio is talking about the **bytes** the dead threads
  leave behind — stacks, TLS slots, per-thread arena
  metadata — and the fact that nothing in the child can
  drive them forward, free them, or even safely walk
  them. That's a memory leak in the strict sense: held
  but unreachable.
- tractor is talking about the **execution** side
  relevant to the forkserver design: which threads
  retire instructions in the child? Exactly one — the
  one that called `fork()`. Everything else, regardless
  of the bytes left behind, is dead in a scheduler
  sense.

Neither framing is wrong; they're just answering
different questions.

## POSIX `fork()` in a multi-threaded program — what actually happens

Per POSIX (and concretely on Linux glibc), the contract
of `fork()` in a multi-threaded process is:

1. The kernel creates a new process whose virtual
   address space is a COW copy of the parent's. *All*
   pages map across — code, heap, every thread's stack,
   every malloc arena, every mmap region.
2. Of the parent's N threads, exactly **one** is
   reified in the child as a runnable kernel task: the
   thread that called `fork()`. The other N-1 threads
   have *no* corresponding task in the child kernel. They
   were never scheduled, never `clone()`d for the child,
   never exist as runnable entities.
3. Their **memory artifacts** — pthread stacks, TLS,
   `pthread_t` structures, glibc per-thread arena
   bookkeeping — are still mapped in the child's address
   space, because (1) duplicates *everything* page-wise.
   They sit there as inert COW bytes.
4. The kernel does not clean those bytes up. There is no
   "phantom-thread cleanup" pass post-fork. The kernel
   doesn't know which mapped pages "belonged to" which
   thread — at the kernel level mappings are
   process-scoped, not thread-scoped.
5. The surviving thread (the caller of `fork()`) cannot
   safely access those leaked bytes either. Any state
   they encoded — held mutexes, in-flight syscalls,
   half-updated invariants — is frozen at whatever
   instant the parent's fork-syscall observed it. Some
   of those mutexes may even still be locked from the
   child's POV (the canonical "fork-in-multithreaded-
   program-deadlocks" hazard; see `man pthread_atfork`).

So: from the kernel's PoV, the child has one thread.
From the address-space's PoV, the child has all the
parent's bytes — including the corpses of the N-1 dead
threads' stacks. Both true simultaneously.

## Why trio says "leaked"

trio's framing makes sense from the parent's
PoV, looking at *what those threads were doing*. In a
running `trio.run()` process you typically have:

- The trio runner thread itself — owns the `selectors`
  epoll fd, the signal-wakeup-fd, the run-queue.
- Threadpool worker threads (`trio.to_thread`'s cache)
  — blocked in `wait()` on the threadpool's work
  condvar.
- Whatever other ad-hoc threads the application
  started.

Each of those threads owns *real work-state*: epoll
registrations, file descriptors held in
soon-to-be-completed reads, half-released locks, posted
but unconsumed wakeups. After fork, that state is still
encoded in the child's memory. None of it is invalid in
a well-formed-bytes sense. It's just that:

- The thread that was driving it is gone.
- Nothing else in the child knows the layout well
  enough to take over.
- Even if it did, the kernel objects backing the work
  (epoll fd, signalfd) have separate post-fork
  semantics that don't compose with userland trio
  state.

So the bytes are *held* (they're in the child's
address space, they count against RSS, they survive
until something clobbers them), and they're
*unreachable* in any meaningful sense — no thread can
safely drive them forward. That is the textbook
definition of a leak.

trio's quote is reminding the user that `fork()` from a
multi-threaded process is a one-way memory hazard:
whatever those threads were doing, that work-state is
now garbage you happen to still be carrying.

## Why tractor says "gone"

tractor's `_main_thread_forkserver` framing is concerned
with a different question: *which thread executes in the
child, and is it safe?*

The forkserver design rests on POSIX's "calling thread
is the sole survivor" guarantee. We pick that calling
thread very deliberately: a dedicated worker that has
provably never entered trio. So the thread that *does*
run in the child is one whose locals, TLS, and stack
contain nothing trio-related. Trio's runner thread —
the one that owned the epoll fd and the run-queue — is
*gone* from the child in the execution sense. It will
never run another instruction. The fact that its stack
bytes still exist in the child's address space (the
"leaked" view) is irrelevant to the forkserver, because
nothing in the child reads or writes those pages.

So when the docstring says "Every other thread … is
gone the instant `fork()` returns in the child", it's
being precise about the surface that matters for the
backend: scheduler-level liveness. Nothing schedules
those threads ever again. Whether their bytes are
hanging around is a separate (and, for the design,
non-load-bearing) fact.

## Cross-table

The same tabular layout the `_main_thread_forkserver`
docstring uses, expanded with a fourth "what handles
it" column:

| thread              | parent    | child (executing) | child (memory)               | what handles it             |
|---------------------|-----------|-------------------|------------------------------|-----------------------------|
| forkserver worker   | continues | sole survivor     | live stack                   | runs the child's bootstrap  |
| `trio.run()` thread | continues | not running       | leaked stack (zombie bytes)  | overwritten by child's fresh `trio.run()` |
| any other thread    | continues | not running       | leaked stack (zombie bytes)  | overwritten / GC'd / clobbered by `exec()` if used |

The "child (executing)" column is the *execution* side
of the coin — what tractor cares about. The "child
(memory)" column is the *memory* side — what trio
cares about.

The "what handles it" column is the deliberate punchline
of the design: nothing has to handle the leaked bytes
*explicitly*. They get clobbered by ordinary forward
progress in the child:

- The fresh `trio.run()` the child boots up allocates
  its own stack, scheduler, and run-queue, which over
  time overlaps and overwrites the inherited zombie
  pages.
- Python's GC walks live objects only; the dead-thread
  Python frames aren't reachable from any
  `PyThreadState`, so they get freed at the next
  collection cycle.
- If the child eventually `exec()`s, the entire address
  space is replaced and the leak vanishes.

## What this means for the forkserver design

The crucial point is that **the design doesn't and
*can't* prevent the leak**. There is no userland fix
for COW thread stacks. The kernel hands the child a
duplicated address space; that's what `fork()` *is*. No
amount of pre-fork hookery, `pthread_atfork()`
gymnastics, or post-fork cleanup can un-COW the dead
threads' pages without unmapping them, and unmapping
arbitrary regions of a duplicated address space is
neither portable nor safe.

What the design *does* ensure is the orthogonal
property: the survivor thread is one that doesn't need
any of that leaked state to function. Concretely:

- Survivor is the forkserver worker thread.
- That worker has provably never imported, called into,
  or held any reference to `trio`. (Enforced by keeping
  the worker's lifecycle entirely in
  `_main_thread_forkserver.py` and never letting trio
  task-state cross into it.)
- So the leaked pages — trio runner stack, threadpool
  caches, etc. — are inert relative to the survivor.
  No code path in the child references them.
- The child then boots its own fresh `trio.run()`,
  which allocates new state in new pages. Over the
  child's lifetime the COW'd zombie pages get
  overwritten, GC'd, or (if the child eventually
  `exec()`s) discarded wholesale.

The "leak" is real but inert. It costs RSS until
clobbered; it doesn't cost correctness. That's exactly
the property the forkserver pattern is built on, and
it's also why the design needs the "calling thread is
trio-free" precondition to be airtight: if the survivor
were a trio thread, it *would* try to drive the leaked
trio state, and the leak would no longer be inert.

## See also

- `tractor/spawn/_main_thread_forkserver.py` — module
  docstring's "What survives the fork? — POSIX
  semantics" section is the in-tree, code-adjacent
  prose this doc expands on. The cross-table here is a
  fourth-column expansion of the table there.

- [python-trio/trio#1614][trio-1614] — the trio issue
  with the "leaked" framing, and the canonical thread
  for trio + `fork()` hazards more broadly.

- [`subint_fork_blocked_by_cpython_post_fork_issue.md`](./subint_fork_blocked_by_cpython_post_fork_issue.md)
  — sibling analysis covering CPython's *post-fork*
  hooks (`PyOS_AfterFork_Child`,
  `_PyInterpreterState_DeleteExceptMain`) and why
  fork-from-non-main-subint is a CPython-level hard
  refusal. Complementary axis: this doc is about POSIX
  semantics; that doc is about the CPython runtime
  layer that runs *after* POSIX `fork()` returns in
  the child.

- `man pthread_atfork(3)` — canonical "fork in a
  multithreaded process is dangerous" reference.
  Especially the rationale section, which is the
  closest thing to a normative statement of "the
  surviving thread cannot safely use anything the dead
  threads were touching."

- `man fork(2)` (Linux) — "Other than [the calling
  thread], … no other threads are replicated …"
  paragraph is the kernel-side statement of the
  execution-side framing this doc opens with.

[trio-1614]: https://github.com/python-trio/trio/issues/1614
