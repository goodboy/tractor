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


def confdir():
    dirname = os.path.dirname
    dirpath = os.path.abspath(
        dirname(dirname(os.path.realpath(__file__)))
        )
    return dirpath


def examples_dir():
    return os.path.join(confdir(), 'examples')


@pytest.fixture
def run_example_in_subproc(loglevel, testdir, arb_addr):

    @contextmanager
    def run(script_code):
        kwargs = dict()

        if platform.system() == 'Windows':
            # on windows we need to create a special __main__.py which will
            # be executed with ``python -m __main__.py`` on windows..
            shutil.copyfile(
                os.path.join(examples_dir(), '__main__.py'),
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


@pytest.mark.parametrize(
    'example_script',
    [f for f in os.listdir(examples_dir()) if '__' not in f],
)
def test_example(run_example_in_subproc, example_script):
    ex_file = os.path.join(examples_dir(), example_script)
    with open(ex_file, 'r') as ex:
        code = ex.read()

        with run_example_in_subproc(code) as proc:
            proc.wait()
            err, _ = proc.stderr.read(), proc.stdout.read()

            # if we get some gnarly output let's aggregate and raise
            if err and b'Error' in err:
                raise Exception(err.decode())

            assert proc.returncode == 0
