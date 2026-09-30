'''
Externally supervised tests for cancellable IPC unresponsiveness.

No kernel packet filtering: these waits model a task making no
progress. EOF remains enabled in production and is never injected.
The watchdog signals only its own root process handle.

'''
from collections.abc import (
    Callable,
    Iterator,
)
from contextlib import (
    contextmanager as cm,
    suppress,
)
from dataclasses import (
    dataclass,
    field,
)
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import psutil
import pytest
from tractor._testing.addr import get_rando_addr


@dataclass
class RootMonitor:
    '''
    Observe one stalled-IPC root outside its actor/IPC tree.

    ``RootMonitor.proc`` is the test-owned root subprocess.
    ``RootMonitor.transcript`` retains plain logs; only lines
    prefixed with ``RootMonitor.marker`` contain JSON records
    from ``examples.advanced_faults.ipc_stalls.emit()``.
    ``RootMonitor.child`` is learned from the peer's PID event,
    so cleanup never selects a process by global name.

    '''
    # TODO: reuse for the ``daemon`` remote registrar in
    # ``tests/discovery/conftest.py`` to verify logged discovery
    # transactions from outside the actor tree.
    # TODO: colocate scenario and monitor behind a reusable
    # ``tractor._testing`` interface for other suites.
    proc: subprocess.Popen[bytes]
    marker: str
    child: psutil.Process|None = None
    transcript: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    deadline: float = field(
        default_factory=lambda: time.monotonic() + 20
    )

    def iter_events(self) -> Iterator[dict[str, Any]]:
        '''
        Yield marker-prefixed records until root stdout closes.

        Poll the independent pipe using ``RootMonitor.deadline``.
        Keep plain actor logs in ``RootMonitor.transcript`` and
        parse only ``emit()`` records into ``RootMonitor.events``.
        A root that cannot publish readiness or finish after
        SIGINT fails the external watchdog, regardless of IPC.

        '''
        stdout = self.proc.stdout
        assert stdout is not None
        pending: bytes = b''
        with selectors.DefaultSelector() as poller:
            poller.register(stdout, selectors.EVENT_READ)
            while poller.get_map():
                remaining: float = (
                    self.deadline - time.monotonic()
                )
                if remaining <= 0:
                    raise TimeoutError('External watchdog expired')
                if not poller.select(remaining):
                    raise TimeoutError(
                        'External watchdog expired'
                    )
                chunk: bytes = os.read(stdout.fileno(), 65536)
                if not chunk:
                    poller.unregister(stdout)
                    break
                pending += chunk

                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    text: str = line.decode(errors='replace')
                    self.transcript.append(text)
                    if not text.startswith(self.marker):
                        continue
                    event: dict[str, Any] = json.loads(
                        text[len(self.marker):]
                    )
                    self.events.append(event)
                    yield event

    def signal_root(
        self,
        sig: int = signal.SIGINT,
    ) -> None:
        '''
        Signal the owned root; bound the ensuing shutdown.

        Default to graceful SIGINT cancellation. ``sig`` also allows
        future scenarios to exercise non-graceful termination.

        '''
        self.proc.send_signal(sig)
        self.deadline = time.monotonic() + 20


@cm
def open_root_monitor(
    mode: str,
) -> Iterator[RootMonitor]:
    '''
    Own one isolated actor program and rescue only failed runs.

    Launch ``examples.advanced_faults.ipc_stalls`` as a separate
    process with a per-test registry port and stdout control pipe.
    Keep ``pytest`` outside both potentially stalled IPC channels
    so it can signal, time out, and inspect the root's OS exit.
    Failure cleanup touches only handles created by this context;
    watchdog rescue never counts as runtime teardown success.

    '''
    root: Path = Path(__file__).resolve().parents[1]
    marker: str = f'IPC-STALL-{uuid4().hex}:'
    # Pure address selection, without ``reg_addr`` runtime overrides.
    # ``ensure_registry`` refuses a collision instead of attaching.
    _, port = get_rando_addr('tcp')

    env: dict[str, str] = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (
        str(root), env.get('PYTHONPATH'),
    )))
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # TODO: generalize the scenario entrypoint together with the
    # ``daemon`` fixture's spawner in
    # ``tests/discovery/conftest.py``.
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        [
            sys.executable,
            '-u',
            '-m', 'examples.advanced_faults.ipc_stalls',
            mode,
            str(port),
            marker,
        ],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    monitor = RootMonitor(proc=proc, marker=marker)
    try:
        yield monitor
    except BaseException as exc:
        output: str = '\n'.join(monitor.transcript)
        # XXX i tweaked this for newlines
        exc.add_note(
            f'Controller events:\n'
            f'{monitor.events!r}\n'
            f'{output}'
        )
        raise
    finally:
        # Failure-only rescue: retained test-owned PIDs, never a
        # global reaper, process group, or unrelated resource sweep.
        owned: list[psutil.Process] = (
            [monitor.child] if monitor.child else []
        )
        if proc.poll() is None:
            # Include children if root stalled before its PID event.
            with suppress(psutil.NoSuchProcess):
                owned.extend(
                    psutil.Process(proc.pid).children(
                        recursive=True,
                    )
                )

        for descendant in set(owned):
            with suppress(psutil.NoSuchProcess):
                if descendant.is_running():
                    descendant.kill()

        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        assert proc.stdout is not None
        proc.stdout.close()

        _, survivors = psutil.wait_procs(owned, timeout=3)
        assert not survivors, (
            'Watchdog cleanup survivors', survivors,
        )


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux watchdog',
)
@pytest.mark.parametrize(
    'mode',
    [
        # Baseline: both context and subactor ``Portal`` IPC work.
        'healthy_ipc',
        # Stall directions on the separate OOB context ``Channel``.
        'stalled_context_receive',
        'stalled_context_send',
        'stalled_context_send_and_receive',
        # Park ``Context.cancel()`` on that OOB context ``Channel``.
        'stalled_context_cancel_send',
        # Both context directions plus subactor ``Portal`` cancel.
        'stalled_context_and_subactor_cancel_send',
        # Shield the peer task; keep subactor control IPC usable.
        'stalled_context_with_shielded_peer',
    ],
)
def test_stalled_channel_sigint(
    mode: str,
    # ``pytest`` provides ``record_property`` via _pytest.junitxml.
    record_property: Callable[[str, object], None],
) -> None:
    '''
    SIGINT must tear down a root with stalled child IPC.

    The example's ``StallableReceiveStream.receive_exactly()`` sets
    ``StallableReceiveStream.parked`` before waiting on
    ``StallableReceiveStream.release``. Peer responses complete any
    pending read inside the original ``BufferedReceiveStream``.
    The reader task's next header or payload read accesses the
    replaced ``MsgpackTransport.recv_stream`` and parks on that
    event; this does not flush buffered bytes.
    ``StallableTransport.send()`` sets
    ``StallableTransport.send_parked`` before waiting on
    ``StallableTransport.release_send``.

    In ``examples.advanced_faults.ipc_stalls.root_main()``, the root
    actor's ``ready()`` closure waits for selected parked-operation
    events and emits ready JSON on stdout. ``RootMonitor`` checks
    that record before delivering SIGINT; readiness does not depend
    on communicating over a root ``Portal``.

    CASES: (parametrized by ``mode``)

    - Subactor ``Portal`` usable: ``staller_ptl`` comes from
      ``an.start_actor()``. The distinct OOB context ``peer_ptl``
      uses a ``Channel`` from ``_connect_chan()`` and
      ``open_portal()``.
      These five cases allow cooperative child cancellation:

      - ``healthy_ipc``: probe traffic, with no stalled operations.
      - ``stalled_context_receive``: park context packet reads.
      - ``stalled_context_send``: park a context send.
      - ``stalled_context_send_and_receive``: park both directions.
      - ``stalled_context_cancel_send``: park the finite-deadline
        ``Context.cancel()`` send during actual teardown.

    - Subactor ``Portal`` stalled:

      - ``stalled_context_and_subactor_cancel_send``: park both
        context directions AND ``staller_ptl``'s finite-deadline
        ``Start(self.cancel)`` send. Require deadline expiration,
        runtime hard-reap logs, SIGKILL exit and completed waitpid.

    - Shielded peer task with usable subactor ``Portal``:

      - ``stalled_context_with_shielded_peer``: park both context
        directions, but complete a control RPC after shield entry.
        ``probe_peer`` enters its inner shield before publishing
        ``Context.started()``.
        Before SIGINT require that child's shield-entry event; after
        SIGINT require its observation of ``Actor._cancel_called``
        and cancelled outer ``Context._scope`` inside the still
        uncancelled shield. Require cancel-ack budget expiry in the
        runtime log, runtime hard-reaping, SIGKILL and waitpid proof.
        ``tractor.runtime._supervise._try_cancel_then_kill()`` calls
        ``Portal.cancel_actor(raise_on_timeout=True)``. Its bounded
        RPC wait raises ``ActorTooSlowError`` in
        ``tractor.runtime._portal``; the supervisor logs escalation
        and calls ``proc.kill()``. The Trio spawner joins via
        ``tractor.spawn._spawn.soft_kill()`` with
        ``trio.Process.wait``; the root's ``os.waitpid()`` check
        independently proves the child was already reaped.
        An unshielded task's clean exit must fail these assertions.
        This models outer-cancel resistance, not a kernel syscall.

    ``open_root_monitor()`` observes OS lifetime outside the actor
    tree. A ``Portal`` within the root cannot independently enforce
    a watchdog deadline or prove OS exit over stalled IPC. Missing
    readiness, premature completion, root timeout or an unreaped
    child fails this test; watchdog rescue cannot count as success.
    Kernel TCP half-open/DROP scenarios remain follow-up work.

    '''
    signalled: bool = False
    result: dict[str, Any]|None = None
    stalls: dict[str, bool]|None = None
    shield_entered: bool = False
    shield_cancel_pending: bool = False
    with open_root_monitor(mode=mode) as monitor:
        proc: subprocess.Popen[bytes] = monitor.proc
        for event in monitor.iter_events():
            if event['event'] == 'child':
                # Retain one OS identity and verify root ownership;
                # later exit checks must concern this exact child.
                assert monitor.child is None
                monitor.child = psutil.Process(event['pid'])
                assert monitor.child.ppid() == proc.pid

            elif event['event'] == 'ready':
                # Prove the child is live and selected operations
                # really parked before sending the sole SIGINT.
                assert not signalled
                child = monitor.child
                assert child is not None
                assert child.is_running()
                stalls = event['stalls']
                assert stalls['receive'] == (
                    mode in {
                        'stalled_context_receive',
                        'stalled_context_send_and_receive',
                        'stalled_context_and_subactor_cancel_send',
                        'stalled_context_with_shielded_peer',
                    }
                )
                assert stalls['send'] == (
                    mode in {
                        'stalled_context_send',
                        'stalled_context_send_and_receive',
                        'stalled_context_cancel_send',
                        'stalled_context_and_subactor_cancel_send',
                        'stalled_context_with_shielded_peer',
                    }
                )
                assert stalls['finite_send'] == (
                    mode == 'stalled_context_cancel_send'
                )
                if mode == (
                    'stalled_context_and_subactor_cancel_send'
                ):
                    assert event['control'] == {
                        'receive': False,
                        'send': True,
                        'finite_send': True,
                    }
                assert proc.poll() is None
                if mode == 'stalled_context_with_shielded_peer':
                    assert shield_entered
                    assert event['shield_peer'] is True
                    assert event['control_usable'] is True
                    assert event['control'] is None
                    assert 0 < event['cancel_timeout'] < 20
                monitor.signal_root()
                signalled = True

            elif event['event'] == 'result':
                # Accept exactly one shutdown result, only after
                # SIGINT; voluntary early completion is a failure.
                assert signalled
                assert result is None
                result = event

            elif event['event'] == 'control_send_exit':
                # Only this case parks the subactor ``Portal`` send.
                # Its finite-budget exit must ultimately time out;
                # the final checks below require that evidence.
                assert mode == (
                    'stalled_context_and_subactor_cancel_send'
                )

            elif event['event'] == 'shield_entered':
                assert mode == 'stalled_context_with_shielded_peer'
                assert not signalled
                assert not shield_entered
                assert monitor.child is not None
                assert event['pid'] == monitor.child.pid
                shield_entered = True

            elif event['event'] == 'shielded_cancel_pending':
                assert mode == 'stalled_context_with_shielded_peer'
                assert signalled
                assert shield_entered
                assert not shield_cancel_pending
                assert monitor.child is not None
                assert event['pid'] == monitor.child.pid
                assert event['actor_cancel_received'] is True
                assert event['outer_cancel_called'] is True
                assert event['shield'] is True
                assert event['shield_cancel_called'] is False
                shield_cancel_pending = True

            else:
                raise AssertionError(event)

        # The root caught SIGINT and reported runtime waitpid proof;
        # these checks precede any failure-only watchdog rescue.
        assert proc.wait(timeout=2) == 0
        assert signalled, 'Root completed before signal readiness'
        assert result is not None
        assert stalls is not None
        child = monitor.child
        assert child is not None
        assert result['root_exit'] == 'KeyboardInterrupt'
        assert result['child_reaped'] is True
        control_expired: bool = any(
            event.get('deadline_expired', False)
            for event in monitor.events
        )
        if mode == 'stalled_context_and_subactor_cancel_send':
            # Both context directions and the subactor ``Portal``
            # cancel send stalled: require actual budget expiry,
            # SIGKILL status and the runtime's hard-reap log.
            assert control_expired
            assert result['child_returncode'] == -signal.SIGKILL
            assert result['control_fault_active'] == {
                'receive': False,
                'send': True,
                'finite_send': True,
            }
            assert any(
                'escalating to `proc.kill()` (hard-reap)' in line
                for line in monitor.transcript
            ), 'Missing runtime hard-reap evidence'
        elif mode == 'stalled_context_with_shielded_peer':
            assert shield_cancel_pending
            assert result['control_fault_active'] is None
            # ``_supervise._try_cancel_then_kill()`` kills the child
            # when its shield prevents the cancel RPC completing:
            # ``proc.kill()`` produces this negative SIGKILL status.
            # ``_spawn.soft_kill()`` joins via ``trio.Process.wait``;
            # ``child_reaped`` above proves the OS wait completed.
            # This log identifies supervisor escalation, distinct
            # from the Trio spawner's legacy ``hard_kill`` fallback.
            assert result['child_returncode'] == -signal.SIGKILL
            assert any(
                'did not ack its `Actor.cancel()` RPC within '
                'bounded wait' in line
                for line in monitor.transcript
            ), 'Missing runtime cancel-ack budget expiry'
            assert any(
                'escalating to `proc.kill()` (hard-reap)' in line
                for line in monitor.transcript
            ), 'Missing runtime hard-reap evidence'
        else:
            # The baseline and context-only faults retain a usable
            # subactor ``Portal``: cooperative child exit is clean.
            assert result['child_returncode'] == 0
        assert result['stalls_fault_active'] == stalls
        assert not child.is_running()
        record_property('ipc_stall_result', json.dumps({
            **result,
            'mode': mode,
            'root_returncode': proc.returncode,
            'watchdog_forced_cleanup': False,
            'runtime_hard_reap': mode in {
                'stalled_context_and_subactor_cancel_send',
                'stalled_context_with_shielded_peer',
            },
            'control_deadline_expired': control_expired,
        }))
