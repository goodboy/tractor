"""
Spawning basics including audit of,

- subproc bootstrap, such as subactor runtime-data/config inheritance,
- basic (and mostly legacy) `ActorNursery` subactor starting and
  cancel APIs.

Simple (and generally legacy) examples from the original
API design.

"""
from functools import partial
from types import SimpleNamespace
from typing import (
    Any,
)
from unittest.mock import (
    AsyncMock,
    MagicMock,
)

import pytest
import trio
import tractor

from tractor._exceptions import ActorFailure
from tractor._testing import tractor_test
from tractor.spawn import (
    _spawn,
    _trio,
)

data_to_pass_down = {
    'doggy': 10,
    'kitty': 4,
}


def test_peer_handshake_wins_child_boot_race() -> None:
    '''
    A connected child must cancel process-death monitoring cleanly.

    Start the death waiter first and hold it at a checkpoint. Let the
    fake server then return one peer event and channel. The helper must
    preserve the normal handshake result and cancel the losing process
    waiter before its nursery exits. The fake explicitly catches and
    re-raises `trio.Cancelled` to prove cancellation caused its exit.

    '''
    async def main() -> None:
        '''
        Control the handshake-first schedule with Trio events.

        '''
        uid: tuple[str, str] = ('handshake-child', 'test')
        death_started = trio.Event()
        death_cancelled = trio.Event()
        peer_event = trio.Event()
        channel = object()

        async def wait_for_peer(
            child_uid: tuple[str, str],
        ) -> tuple[trio.Event, object]:
            '''
            Return the peer only after death monitoring is active.

            '''
            assert child_uid == uid
            await death_started.wait()
            return (peer_event, channel)

        async def wait_for_death() -> int:
            '''
            Block until the winning handshake cancels this waiter.

            '''
            death_started.set()
            try:
                await trio.sleep_forever()
            except trio.Cancelled:
                death_cancelled.set()
                raise

        result = await _spawn.wait_for_peer_or_proc_death(
            ipc_server=SimpleNamespace(
                wait_for_peer=wait_for_peer,
            ),
            uid=uid,
            proc_wait=wait_for_death,
            proc_repr='handshake-proc',
        )

        assert result == (peer_event, channel)
        assert death_cancelled.is_set()

    trio.run(main)


def test_child_death_wins_peer_handshake_race() -> None:
    '''
    Pre-handshake child death must fail startup instead of hanging.

    Start the peer waiter first and leave it parked like
    `IPCServer.wait_for_peer()` on an unset event. Return a distinctive
    process status from the competing waiter, then prove the helper
    cancels the handshake and raises `ActorFailure` with child identity,
    status, and process diagnostics.

    '''
    async def main() -> None:
        '''
        Control the death-first schedule with Trio events.

        '''
        uid: tuple[str, str] = ('dead-child', 'test')
        handshake_started = trio.Event()
        handshake_cancelled = trio.Event()

        async def wait_for_peer(
            child_uid: tuple[str, str],
        ) -> tuple[trio.Event, object]:
            '''
            Park until process death cancels this handshake waiter.

            '''
            assert child_uid == uid
            handshake_started.set()
            try:
                await trio.sleep_forever()
            except trio.Cancelled:
                handshake_cancelled.set()
                raise

        async def wait_for_death() -> int:
            '''
            Report child death after handshake monitoring is active.

            '''
            await handshake_started.wait()
            return 23

        with pytest.raises(ActorFailure) as exc_info:
            await _spawn.wait_for_peer_or_proc_death(
                ipc_server=SimpleNamespace(
                    wait_for_peer=wait_for_peer,
                ),
                uid=uid,
                proc_wait=wait_for_death,
                proc_repr='dead-proc',
            )

        message: str = str(exc_info.value)
        assert repr(uid) in message
        assert 'died during boot' in message
        assert '(rc=23)' in message
        assert 'parent-handshake' in message
        assert 'dead-proc' in message
        assert handshake_cancelled.is_set()

    trio.run(main)


def test_child_death_wins_simultaneous_boot_results() -> None:
    '''
    Observed process death must outrank a simultaneous handshake.

    Hold both fake waits behind one barrier with cancellation shielding,
    then release them together so both publish a committed result before
    sibling cancellation takes effect. Because the child has exited
    before receiving its `SpawnSpec`, bootstrap must raise `ActorFailure`
    rather than return its briefly established channel.

    '''
    async def main() -> None:
        '''
        Release both boot outcomes from one controlled barrier.

        '''
        uid: tuple[str, str] = ('simultaneous-child', 'test')
        handshake_ready = trio.Event()
        death_ready = trio.Event()
        release = trio.Event()
        peer_event = trio.Event()

        async def wait_for_peer(
            child_uid: tuple[str, str],
        ) -> tuple[trio.Event, object]:
            '''
            Publish a handshake despite sibling cancellation.

            '''
            assert child_uid == uid
            handshake_ready.set()
            with trio.CancelScope(shield=True):
                await release.wait()
            return (peer_event, object())

        async def wait_for_death() -> int:
            '''
            Publish process death despite sibling cancellation.

            '''
            death_ready.set()
            with trio.CancelScope(shield=True):
                await release.wait()
            return 0

        async def release_both() -> None:
            '''
            Open the barrier only after both waiters are parked.

            '''
            await handshake_ready.wait()
            await death_ready.wait()
            release.set()

        async with trio.open_nursery() as nursery:
            nursery.start_soon(release_both)
            with pytest.raises(
                ActorFailure,
                match=r'simultaneous-child.*rc=0',
            ):
                await _spawn.wait_for_peer_or_proc_death(
                    ipc_server=SimpleNamespace(
                        wait_for_peer=wait_for_peer,
                    ),
                    uid=uid,
                    proc_wait=wait_for_death,
                )

    trio.run(main)


@pytest.mark.parametrize('failing_waiter', ('handshake', 'death'))
def test_child_boot_race_preserves_waiter_error(
    failing_waiter: str,
) -> None:
    '''
    Waiter failures must retain their original exception identity.

    Park the non-failing sibling and raise one unique error from either
    the peer or process waiter. The helper's internal nursery must
    cancel the sibling and re-raise that exact exception instead of
    wrapping it in an `ExceptionGroup`.

    '''
    async def main() -> None:
        '''
        Trigger one selected waiter after its sibling starts.

        '''
        uid: tuple[str, str] = ('errored-child', 'test')
        sibling_started = trio.Event()
        wait_error = RuntimeError(f'{failing_waiter} failed')

        async def wait_for_peer(
            child_uid: tuple[str, str],
        ) -> tuple[trio.Event, object]:
            '''
            Raise or park according to the selected peer schedule.

            '''
            assert child_uid == uid
            if failing_waiter == 'handshake':
                await sibling_started.wait()
                raise wait_error

            sibling_started.set()
            await trio.sleep_forever()

        async def wait_for_death() -> int:
            '''
            Raise or park according to the selected process schedule.

            '''
            if failing_waiter == 'death':
                await sibling_started.wait()
                raise wait_error

            sibling_started.set()
            await trio.sleep_forever()

        with pytest.raises(RuntimeError) as exc_info:
            await _spawn.wait_for_peer_or_proc_death(
                ipc_server=SimpleNamespace(
                    wait_for_peer=wait_for_peer,
                ),
                uid=uid,
                proc_wait=wait_for_death,
            )

        assert exc_info.value is wait_error

    trio.run(main)


def test_trio_proc_cleans_failed_child_peer_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Death-first Trio startup must release provisional peer state.

    Return one already-dead fake process while its server handshake
    parks forever. The fake nursery proves the peer event exists before
    provisional child publication. After `ActorFailure`, both that
    exact event and the provisional child record must be gone so repeated
    failed spawns cannot leak server state.

    '''
    uid: tuple[str, str] = ('dead-trio-child', 'test')
    proc: trio.Process = MagicMock(spec=trio.Process)
    proc.pid = 1234
    proc.wait = AsyncMock(return_value=23)
    proc.poll.return_value = 23
    proc.__str__.return_value = 'dead-trio-proc'

    class FakeServer:
        '''
        Hold the peer registry used during Trio child startup.

        '''
        def __init__(self) -> None:
            self._peer_connected: dict[
                tuple[str, str],
                trio.Event,
            ] = {}

        async def wait_for_peer(
            self,
            child_uid: tuple[str, str],
        ) -> tuple[trio.Event, object]:
            '''
            Park like a child that never reaches its handshake.

            '''
            assert child_uid == uid
            await trio.sleep_forever()

    server = FakeServer()

    class FakeNursery:
        '''
        Track provisional child publication and cleanup.

        '''
        def __init__(self) -> None:
            self._actor = SimpleNamespace(ipc_server=server)
            self._children: dict[tuple[str, str], tuple] = {}

        def _register_child(
            self,
            subactor: object,
            proc: object,
            portal: object|None,
        ) -> tuple[trio.Event, trio.Event, bool]:
            '''
            Require peer-event registration before child publication.

            '''
            assert uid in server._peer_connected
            assert portal is None
            self._children[uid] = (subactor, proc, portal)
            return (trio.Event(), trio.Event(), False)

    async def fake_open_process(
        command: list[str],
        **kwargs: object,
    ) -> trio.Process:
        '''
        Return a process whose death wins the bootstrap race.

        '''
        assert command
        return proc

    async def fake_wait_for_debugger(**kwargs: object) -> None:
        '''
        Keep hard-reap cleanup deterministic and non-interactive.

        '''
        return None

    monkeypatch.setattr(
        _trio.trio.lowlevel,
        'open_process',
        fake_open_process,
    )
    monkeypatch.setattr(
        _trio.debug,
        'maybe_wait_for_debugger',
        fake_wait_for_debugger,
    )

    nursery = FakeNursery()
    subactor = SimpleNamespace(
        aid=tractor.msg.Aid(
            name=uid[0],
            uuid=uid[1],
        ),
        loglevel=None,
        pformat=lambda: 'dead-trio-child',
    )

    async def main() -> None:
        '''
        Run the full Trio backend through death-first cleanup.

        '''
        with pytest.raises(
            ActorFailure,
            match=r'dead-trio-child.*rc=23',
        ):
            await _trio.trio_proc(
                name=uid[0],
                actor_nursery=nursery,
                subactor=subactor,
                errors={},
                bind_addrs=[],
                parent_addr=('127.0.0.1', 1616),
                _runtime_vars={},
            )

    trio.run(main)

    assert server._peer_connected == {}
    assert nursery._children == {}


async def run_same_func_in_child(
    should_be_root: bool,
    data: dict,
    reg_addr: tuple[str, int],

    debug_mode: bool = False,
):
    '''
    Invoke this same module-scoped RPC target in a child actor.

    RPC targets cross IPC as `module:name` namespace paths, so this
    helper must remain import-addressable at module scope instead of
    being nested inside the test. The root invocation boots a runtime
    and recursively calls this function as a one-shot child endpoint;
    the child branch returns the result.

    '''
    await trio.sleep(0.1)
    actor = tractor.current_actor(err_on_no_runtime=False)

    if not should_be_root:
        assert actor is not None
        assert actor.is_registrar == should_be_root
        return 10

    assert actor is None  # no runtime yet
    async with (
        tractor.open_root_actor(
            registry_addrs=[reg_addr],
        ),
        tractor.open_nursery() as an,
    ):
        # now runtime exists
        actor: tractor.Actor = tractor.current_actor()
        assert actor.is_registrar == should_be_root

        # recursively spawn this same function as the lone
        # task of a one-shot child subactor and get its result.
        result = await tractor.to_actor.run(
            partial(
                run_same_func_in_child,
                should_be_root=False,
                data=data_to_pass_down,
                reg_addr=reg_addr,
            ),
            an=an,

            # spawning args
            name='sub-actor',
            enable_modules=[__name__],

        )
        assert result == 10
        return result


def test_to_actor_run_same_func_in_child(
    reg_addr: tuple,
    debug_mode: bool,
):
    result = trio.run(
        partial(
            run_same_func_in_child,
            should_be_root=True,
            data=data_to_pass_down,
            reg_addr=reg_addr,
            debug_mode=debug_mode,
        )
    )
    assert result == 10


async def movie_theatre_question():
    '''
    A question asked in a dark theatre, in a tangent
    (errr, I mean different) process.

    '''
    return 'have you ever seen a portal?'


@tractor_test
async def test_movie_theatre_convo(
    start_method: str,
):
    '''
    The main ``tractor`` routine.

    '''
    async with tractor.open_nursery(debug_mode=True) as an:

        portal = await an.start_actor(
            'frank',
            # enable the actor to run funcs from this current module
            enable_modules=[__name__],
        )

        print(await portal.run(movie_theatre_question))
        # call the subactor a 2nd time
        print(await portal.run(movie_theatre_question))

        # the async with will block here indefinitely waiting
        # for our actor "frank" to complete, we cancel 'frank'
        # to avoid blocking indefinitely
        await portal.cancel_actor()


async def cellar_door(
    return_value: str|None,
):
    return return_value


@pytest.mark.parametrize(
    'return_value', ["Dang that's beautiful", None],
    ids=['return_str', 'return_None'],
)
@tractor_test
async def test_most_beautiful_word(
    start_method: str,
    return_value: Any,
    debug_mode: bool,
):
    '''
    The main ``tractor`` routine.

    '''
    # actor spawn + IPC round-trip is comfortably sub-second on a
    # warm box, but slow/noisy CI runners (esp. macOS) blow a flat
    # 1s deadline. Scale for CI/CPU-throttle headroom — `== 1s`
    # locally where `cpu_perf_headroom()` is `1.0`.
    from .conftest import cpu_perf_headroom
    with trio.fail_after(1 * cpu_perf_headroom()):
        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as an:
            res: Any = await tractor.to_actor.run(
                partial(
                    cellar_door,
                    return_value=return_value,
                ),
                an=an,
                name='some_linguist',
            )
            assert res == return_value
    # The ``async with`` unblocks here — the 'some_linguist'
    # one-shot actor completed its lone task ``cellar_door`` and
    # was reaped by `to_actor.run()`.
    print(res)


async def check_loglevel(level):
    assert tractor.current_actor().loglevel == level
    log = tractor.log.get_logger()
    # XXX using a level actually used inside tractor seems to trigger
    # some kind of `logging` module bug FYI.
    log.critical('yoyoyo')


@pytest.mark.parametrize(
    'level', [
        'debug',
        'cancel',
        'critical'
    ],
    ids='loglevel={}'.format,
)
def test_loglevel_propagated_to_subactor(
    capfd: pytest.CaptureFixture,
    start_method: str,
    reg_addr: tuple,
    level: str,
):
    if start_method in ('mp_forkserver', 'main_thread_forkserver'):
        pytest.skip(
            "a bug with `capfd` seems to make forkserver capture not work? "
            "(same class as the `mp_forkserver` pre-existing skip — fork-"
            "based backends inherit pytest's capfd temp-file fds into the "
            "subactor and the IPC handshake reads garbage (`unclean EOF "
            "read only X/HUGE_NUMBER bytes`). Work around by using "
            "`capsys` instead or skip entirely."
        )

    async def main():
        async with tractor.open_nursery(
            name='registrar',
            start_method=start_method,
            registry_addrs=[reg_addr],

        ) as an:
            await tractor.to_actor.run(
                partial(
                    check_loglevel,
                    level=level,
                ),
                an=an,
                loglevel=level,
            )

    trio.run(main)

    # ensure subactor spits log message on stderr
    captured = capfd.readouterr()
    assert 'yoyoyo' in captured.err


async def check_parent_main_inheritance(
    expect_inherited: bool,
) -> bool:
    '''
    Assert that the child actor's ``_parent_main_data`` matches the
    ``inherit_parent_main`` flag it was spawned with.

    With the trio spawn backend the parent's ``__main__`` bootstrap
    data is captured and forwarded to each child so it can replay
    the parent's ``__main__`` as ``__mp_main__``, mirroring the
    stdlib ``multiprocessing`` bootstrap:
    https://docs.python.org/3/library/multiprocessing.html#the-spawn-and-forkserver-start-methods

    When ``inherit_parent_main=False`` the data dict is empty
    (``{}``) so no fixup ever runs and the child keeps its own
    ``__main__`` untouched.

    NOTE: under `pytest` the parent ``__main__`` is
    ``pytest.__main__`` whose ``_fixup_main_from_name()`` is a no-op
    (the name ends with ``.__main__``), so we cannot observe
    a difference in ``sys.modules['__main__'].__name__`` between the
    two modes.  Checking ``_parent_main_data`` directly is the most
    reliable verification that the flag is threaded through
    correctly; a ``RemoteActorError[AssertionError]`` propagates on
    mismatch.

    '''
    import tractor
    actor: tractor.Actor = tractor.current_actor()
    has_data: bool = bool(actor._parent_main_data)
    assert has_data == expect_inherited, (
        f'Expected _parent_main_data to be '
        f'{"non-empty" if expect_inherited else "empty"}, '
        f'got: {actor._parent_main_data!r}'
    )
    return has_data


def test_to_actor_run_can_skip_parent_main_inheritance(
    start_method: str,  # <- only support on `trio` backend rn.
):
    '''
    Verify ``inherit_parent_main=False`` on ``to_actor.run()``
    prevents parent ``__main__`` data from reaching the child.

    '''
    if start_method != 'trio':
        pytest.skip(
            'parent main-inheritance opt-out only affects the trio backend'
        )

    async def main():
        async with tractor.open_nursery(start_method='trio') as an:

            # Default: child receives parent __main__ bootstrap data
            await tractor.to_actor.run(
                partial(
                    check_parent_main_inheritance,
                    expect_inherited=True,
                ),
                an=an,
                name='replaying-parent-main',
            )

            # Opt-out: child gets no parent __main__ data
            await tractor.to_actor.run(
                partial(
                    check_parent_main_inheritance,
                    expect_inherited=False,
                ),
                an=an,
                name='isolated-parent-main',
                inherit_parent_main=False,
            )

    trio.run(main)


def test_start_actor_can_skip_parent_main_inheritance(
    start_method: str,  # <- only support on `trio` backend rn.
):
    '''
    Verify ``inherit_parent_main=False`` on ``start_actor()``
    prevents parent ``__main__`` data from reaching the child.

    '''
    if start_method != 'trio':
        pytest.skip(
            'parent main-inheritance opt-out only affects the trio backend'
        )

    async def main():
        async with tractor.open_nursery(start_method='trio') as an:

            # Default: child receives parent __main__ bootstrap data
            replaying = await an.start_actor(
                'replaying-parent-main',
                enable_modules=[__name__],
            )
            result = await replaying.run(
                check_parent_main_inheritance,
                expect_inherited=True,
            )
            assert result is True
            await replaying.cancel_actor()

            # Opt-out: child gets no parent __main__ data
            isolated = await an.start_actor(
                'isolated-parent-main',
                enable_modules=[__name__],
                inherit_parent_main=False,
            )
            result = await isolated.run(
                check_parent_main_inheritance,
                expect_inherited=False,
            )
            assert result is False
            await isolated.cancel_actor()

    trio.run(main)
