"""
Let's make sure them docs work yah?
"""
from contextlib import contextmanager
import os
import sys
import subprocess
import platform
import shutil

import pytest


@pytest.fixture(scope='session')
def confdir():
    dirname = os.path.dirname
    dirpath = os.path.abspath(
        dirname(dirname(os.path.realpath(__file__)))
        )
    return dirpath


@pytest.fixture
def examples_dir(confdir):
    return os.path.join(confdir, 'examples')


@pytest.fixture
def run_example_in_subproc(loglevel, testdir, arb_addr, examples_dir):

    @contextmanager
    def run(script_code):
        kwargs = dict()

        if platform.system() == 'Windows':
            # on windows we need to create a special __main__.py which will
            # be executed with ``python -m __main__.py`` on windows..
            shutil.copyfile(
                os.path.join(examples_dir, '__main__.py'),
                os.path.join(str(testdir), '__main__.py')
            )

            # drop the ``if __name__ == '__main__'`` guard
            script_code = '\n'.join(script_code.splitlines()[:-4])
            script_file = testdir.makefile('.py', script_code)

            # without this, tests hang on windows forever
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

            # run the "libary module" as a script
            cmdargs = [
                sys.executable,
                '-m',
                # use the "module name" of this "package"
                'test_example'
            ]
        else:
            script_file = testdir.makefile('.py', script_code)
            cmdargs = [
                sys.executable,
                str(script_file),
            ]

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


def test_example(examples_dir, run_example_in_subproc):
    ex_file = os.path.join(examples_dir, 'a_trynamic_first_scene.py')
    with open(ex_file, 'r') as ex:
        code = ex.read()

        with run_example_in_subproc(code) as proc:
            proc.wait()
            err, _ = proc.stderr.read(), proc.stdout.read()

            # if we get some gnarly output let's aggregate and raise
            if err and b'Error' in err:
                raise Exception(err.decode())

            assert proc.returncode == 0
