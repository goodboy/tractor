"""
Let's make sure them docs work yah?
"""
from contextlib import contextmanager
import os
import sys
import subprocess
import platform
import pprint

import pytest


@pytest.fixture(scope='session')
def confdir():
    dirname = os.path.dirname
    dirpath = os.path.abspath(
        dirname(dirname(os.path.realpath(__file__)))
        )
    return dirpath


@pytest.fixture
def run_example_in_subproc(loglevel, testdir, arb_addr):

    @contextmanager
    def run(script_code):
        script_file = testdir.makefile('.py', script_code)
        cmdargs = [
            sys.executable,
            str(script_file),
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
        yield proc
        proc.wait()
        assert proc.returncode == 0

    yield run


def test_a_trynamic_first_scene(confdir, run_example_in_subproc):
    ex_file = os.path.join(confdir, 'examples', 'a_trynamic_first_scene.py')
    with open(ex_file, 'r') as ex:
        code = ex.read()

        with run_example_in_subproc(code) as proc:
            proc.wait()
            err, _ = proc.stderr.read(), proc.stdout.read()

            # if we get some gnarly output let's aggregate and raise
            if err and b'Error' in err:
                raise Exception(err.decode())

            assert proc.returncode == 0
