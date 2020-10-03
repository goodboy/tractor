"""
Let's make sure them docs work yah?
"""
from contextlib import contextmanager
import itertools
import os
import sys
import subprocess
import platform
import shutil

import pytest


def repodir():
    """Return the abspath to the repo directory.
    """
    dirname = os.path.dirname
    dirpath = os.path.abspath(
        dirname(dirname(os.path.realpath(__file__)))
        )
    return dirpath


def examples_dir():
    """Return the abspath to the examples directory.
    """
    return os.path.join(repodir(), 'examples')


@pytest.fixture
def run_example_in_subproc(loglevel, testdir, arb_addr):

    @contextmanager
    def run(script_code):
        kwargs = dict()

        if platform.system() == 'Windows':
            # on windows we need to create a special __main__.py which will
            # be executed with ``python -m <modulename>`` on windows..
            shutil.copyfile(
                os.path.join(examples_dir(), '__main__.py'),
                os.path.join(str(testdir), '__main__.py')
            )

            # drop the ``if __name__ == '__main__'`` guard onwards from
            # the *NIX version of each script
            windows_script_lines = itertools.takewhile(
                lambda line: "if __name__ ==" not in line,
                script_code.splitlines()
            )
            script_code = '\n'.join(windows_script_lines)
            script_file = testdir.makefile('.py', script_code)

            # without this, tests hang on windows forever
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

            # run the testdir "libary module" as a script
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
    [
        f for f in os.listdir(examples_dir())
        if (
            ('__' not in f) and
            ('debugging' not in f)
        )
    ],
)
def test_example(run_example_in_subproc, example_script):
    """Load and run scripts from this repo's ``examples/`` dir as a user
    would copy and pasing them into their editor.

    On windows a little more "finessing" is done to make
    ``multiprocessing`` play nice: we copy the ``__main__.py`` into the
    test directory and invoke the script as a module with ``python -m
    test_example``.
    """
    ex_file = os.path.join(examples_dir(), example_script)
    with open(ex_file, 'r') as ex:
        code = ex.read()

        with run_example_in_subproc(code) as proc:
            proc.wait()
            err, _ = proc.stderr.read(), proc.stdout.read()

            # if we get some gnarly output let's aggregate and raise
            errmsg = err.decode()
            errlines = errmsg.splitlines()
            if err and 'Error' in errlines[-1]:
                raise Exception(errmsg)

            assert proc.returncode == 0
