"""
Multiple python programs invoking ``tractor.run()``
"""
import sys
import time
import signal
import subprocess

import pytest
import tractor


from conftest import tractor_test


def sig_prog(proc, sig):
    "Kill the actor-process with ctr-c."
    proc.send_signal(sig)
    ret = proc.wait()
    assert ret


@pytest.fixture
def daemon(loglevel, testdir, arb_addr):
    cmdargs = [
        sys.executable, '-c',
        "import tractor; tractor.run_daemon(arbiter_addr={}, loglevel={})"
        .format(
            arb_addr,
            "'{}'".format(loglevel) if loglevel else None)
    ]
    proc = testdir.popen(
        cmdargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert not proc.returncode
    time.sleep(0.2)
    yield proc
    # TODO: why sometimes does SIGINT not work on teardown?
    sig_prog(proc, signal.SIGINT)


def test_abort_on_sigint(daemon):
    assert daemon.returncode is None
    time.sleep(0.1)
    sig_prog(daemon, signal.SIGINT)
    assert daemon.returncode == 1
    # XXX: oddly, couldn't get capfd.readouterr() to work here?
    assert "KeyboardInterrupt" in str(daemon.stderr.read())


@tractor_test
async def test_cancel_remote_arbiter(daemon, arb_addr):
    async with tractor.get_arbiter(*arb_addr) as portal:
        await portal.cancel_actor()

    time.sleep(0.1)
    # the arbiter channel server is cancelled but not its main task
    assert daemon.returncode is None

    # no arbiter socket should exist
    with pytest.raises(OSError):
        async with tractor.get_arbiter(*arb_addr) as portal:
            pass


@tractor_test
async def test_register_duplicate_name(daemon):
    async with tractor.open_nursery() as n:
        p1 = await n.start_actor('doggy')
        p2 = await n.start_actor('doggy')

        async with tractor.wait_for_actor('doggy') as portal:
            assert portal.channel.uid in (p2.channel.uid, p1.channel.uid)

        await n.cancel()
