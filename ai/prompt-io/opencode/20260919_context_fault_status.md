---
model: openai/gpt-6-astra
service: opencode
scope: code
substantive: true
git_ref: dd758da5
raw_file: 20260919_context_fault_status.raw.md
---

## Prompt and human design

The human prototyped ContextCancelReason, ContextStatus, and a rename of
the context-owned nursery. They requested implementation after saving
the IB fixes, on a separate downstream branch, with Trio's minimum
version in a separate commit. Their design separates runtime-layer
transport faults from reported remote task failures/cancellations.

Design references supplied by the human:
https://github.com/goodboy/tractor/issues/400
https://github.com/goodboy/tractor/issues/22
https://github.com/goodboy/tractor/issues/368
https://github.com/goodboy/tractor/issues/388

## Response summary

Context._status is the single store for outcome, cancellation protocol,
and handshake/lifecycle state. On explicit human instruction, the
agent removed the generated typed descriptors and duplicate Context
declarations. Internal accesses now use Context._status directly;
there is no moved-field constructor compatibility layer. Policy and
live resources remain on Context. Status diagnostics render only
declared state. Public properties, wait_for_result(), and the deprecated
result() method remain available.

ContextCancelReason is shallow-frozen and includes context/actor/peer
identity, an opaque scope token and explicit fault origin. The retained
exception is mutable and may retain traceback resources. Local validation
and boxed remote errors remain distinguishable even with equal peer IDs.
Raw transport loss no longer causes self-cancel classification to read
remote-error-only attributes before checking its exception type.

## Files changed

- tractor/_context.py: status extraction, metadata and direct access.
- tractor/_exceptions.py: preserve local validation provenance.
- tractor/runtime/_rpc.py: nursery rename and direct status access.
- tractor/_streaming.py: stream state and result status access.
- tractor/msg/_ops.py: final message drain status access.
- tractor/runtime/_runtime.py: Context cancellation status access.
- tractor/devx/debug/_trace.py: copied nursery rename documentation.
- tractor/devx/debug/_tty_lock.py: copied nursery rename documentation.
- tests/test_context_status.py: twenty reason/state/public API cases.
- tests/test_context_stream_semantics.py: status assertions and the
  overrun test double's ContextStatus allocation.
- tests/test_ipc_eof.py: status assertions.
- tests/msg/test_pldrx_limiting.py: status assertions.
- tests/test_inter_peer_cancellation.py: status assertions.

## Verification

The initial implementation passed 86 tests: 16 context-status,
65 context-stream semantics and 5 EOF cases. Used the existing original
worktree interpreter with Trio0.34.0 and per-process source overrides;
no installed packages or live services were changed. Automatic reaping
was disabled. Broader cancellation suites needing reaper-provided
fixtures remain unverified.

After the human-directed simplification, the targeted run passed 91
tests: 20 context-status, 65 context-stream semantics, 5 EOF, and the
overlapping service-nursery cancellation regression. The first run
found a SimpleNamespace overrun test double still declaring
_stream_opened directly; it now allocates ContextStatus. The complete
targeted run passed after that correction and removal of mk_context's
redundant result initialization.

A separate run passed all 8 payload-limiting cases. All 7 cases in
test_inter_peer_cancellation.py stopped at fixture setup because
track_orphaned_uds_per_test belongs to the disabled reaper plugin.
No cleanup plugin was enabled to bypass this constraint.

Both runs used the existing ib_disconnect_cleanup/py313/bin/python,
Trio 0.34.0, and PYTHONPATH ordered as this worktree, that interpreter's
site-packages, then piker/py313 site-packages. TRACTOR_SPAWN_METHOD and
TRACTOR_LOGLEVEL were unset. Pytest arguments, after python -m pytest:

```text
-p no:tractor._testing._reap
tests/test_context_status.py
tests/test_context_stream_semantics.py
tests/test_ipc_eof.py
tests/test_resource_cache.py::test_moc_service_nursery_cancellation_completes_exit
-q
```

Separate process:

```text
-p no:tractor._testing._reap
tests/msg/test_pldrx_limiting.py
tests/test_inter_peer_cancellation.py
-q
```

The source audit accounts for all 70 old production direct accesses:
49 rewritten in _context.py, its redundant mk_context assignment
removed, and 20 rewritten across seven functions in four other files.
All 33 old direct test accesses across five files were addressed:
29 migrated and four compatibility assertions replaced with status
initialization/isolation and public API assertions. The additional
test-double keyword dependency was found by execution and corrected.
Remaining same-named attributes belong to Actor, ActorNursery or
Channel. Added Python lines fit 69 columns; git diff --check passes.

A private pre-adjustment index tree and the real index SHA256 are
recorded in ignored .claude runtime state for review. The existing
plan-build helper captured the private tree without git add. The
real index and HEAD remain unchanged; all remediation is unstaged.

## Human contribution accounting

The prototype classes, reason= experiment, nursery rename, downstream
sequencing, fault-layer distinction, and separate dependency boundary
were human-directed. The human accepted immutable identity metadata
and excluding policies/resources from the status store. The agent added
the actual authoritative store, descriptor delegation, provenance
classification, diagnostics and regressions in the initial output.

The human subsequently rejected descriptor delegation and its
self-imposed constructor/attribute compatibility tests. They directed
the single-store simplification, the precise 11-field migration scope,
preservation of public APIs and the deprecated result method, fault
origin policy, comments/TODO markers and _scope_tn rename. They also
required preserving their staged prototype and leaving the remediation
unstaged. These are human-directed agent edits, not direct manual
source edits. The original raw generated record is retained unchanged.
Acceptance remains human-owned.

## Limits

This does not add new supervision/reconnect APIs, inject Cancelled
subclasses, or selectively uncancel scopes. First cancellation reason
does not necessarily track the latest remote error. copy.copy(Context)
shares the status object, as it also shares other live context resources.
Private-field constructor compatibility and dataclasses.replace()
compatibility assertions were removed on human instruction.
