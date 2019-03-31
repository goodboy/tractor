"""
Multiple python programs invoking ``tractor.run()``
"""
import platform
import sys
import time
import signal
import subprocess

import pytest
import tractor
from conftest import tractor_test

# Sending signal.SIGINT on subprocess fails on windows. Use CTRL_* alternatives
if platform.system() == 'Windows':
    _KILL_SIGNAL = signal.CTRL_BREAK_EVENT
    _INT_SIGNAL = signal.CTRL_C_EVENT
    _INT_RETURN_CODE = 3221225786
    _PROC_SPAWN_WAIT = 2
else:
    _KILL_SIGNAL = signal.SIGKILL
    _INT_SIGNAL = signal.SIGINT
    _INT_RETURN_CODE = 1
    _PROC_SPAWN_WAIT = 0.6 if sys.version_info < (3, 7) else 0.4


def sig_prog(proc, sig):
    "Kill the actor-process with ``sig``."
    proc.send_signal(sig)
    time.sleep(0.1)
    if not proc.poll():
        # TODO: why sometimes does SIGINT not work on teardown?
        # seems to happen only when trace logging enabled?
        proc.send_signal(_KILL_SIGNAL)
    ret = proc.wait()
    assert ret


@pytest.fixture
def daemon(loglevel, testdir, arb_addr):
    cmdargs = [
        sys.executable, '-c',
        "import tractor; tractor.run_daemon((), arbiter_addr={}, loglevel={})"
        .format(
            arb_addr,
            "'{}'".format(loglevel) if loglevel else None)
    ]
    kwargs = dict()
    if platform.system() == 'Windows':
        # without this, tests hang on windows forever
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = testdir.popen(
        cmdargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    assert not proc.returncode
    time.sleep(_PROC_SPAWN_WAIT)
    yield proc
    sig_prog(proc, _INT_SIGNAL)


def test_abort_on_sigint(daemon):
    assert daemon.returncode is None
    time.sleep(0.1)
    sig_prog(daemon, _INT_SIGNAL)
    assert daemon.returncode == _INT_RETURN_CODE
    # XXX: oddly, couldn't get capfd.readouterr() to work here?
    if platform.system() != 'Windows':
        # don't check stderr on windows as its empty when sending CTRL_C_EVENT
        assert "KeyboardInterrupt" in str(daemon.stderr.read())


@tractor_test
async def test_cancel_remote_arbiter(daemon, arb_addr):
    assert not tractor.current_actor().is_arbiter
    async with tractor.get_arbiter(*arb_addr) as portal:
        await portal.cancel_actor()

    time.sleep(0.1)
    # the arbiter channel server is cancelled but not its main task
    assert daemon.returncode is None

    # no arbiter socket should exist
    with pytest.raises(OSError):
        async with tractor.get_arbiter(*arb_addr) as portal:
            pass


def test_register_duplicate_name(daemon, arb_addr):

    async def main():
        assert not tractor.current_actor().is_arbiter
        async with tractor.open_nursery() as n:
            p1 = await n.start_actor('doggy')
            p2 = await n.start_actor('doggy')

            async with tractor.wait_for_actor('doggy') as portal:
                assert portal.channel.uid in (p2.channel.uid, p1.channel.uid)

            await n.cancel()

    # run it manually since we want to start **after**
    # the other "daemon" program
    tractor.run(main, arbiter_addr=arb_addr)
