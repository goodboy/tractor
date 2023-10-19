'''
Let's make sure them docs work yah?

'''
from contextlib import contextmanager
import itertools
import os
import sys
import subprocess
import platform
import shutil

import pytest
from tractor._testing import (
    examples_dir,
)


@pytest.fixture
def run_example_in_subproc(
    loglevel: str,
    testdir,
    reg_addr: tuple[str, int],
):

    @contextmanager
    def run(script_code):
        kwargs = dict()

        if platform.system() == 'Windows':
            # on windows we need to create a special __main__.py which will
            # be executed with ``python -m <modulename>`` on windows..
            shutil.copyfile(
                examples_dir() / '__main__.py',
                str(testdir / '__main__.py'),
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

        # XXX: BE FOREVER WARNED: if you enable lots of tractor logging
        # in the subprocess it may cause infinite blocking on the pipes
        # due to backpressure!!!
        proc = testdir.popen(
            cmdargs,
            **kwargs,
        )
        assert not proc.returncode
        yield proc
        proc.wait()
        assert proc.returncode == 0

    yield run


@pytest.mark.parametrize(
    'example_script',

    # walk yields: (dirpath, dirnames, filenames)
    [
        (p[0], f) for p in os.walk(examples_dir()) for f in p[2]

        if '__' not in f
        and f[0] != '_'
        and 'debugging' not in p[0]
        and 'integration' not in p[0]
        and 'advanced_faults' not in p[0]
        and 'multihost' not in p[0]
    ],

    ids=lambda t: t[1],
)
def test_example(run_example_in_subproc, example_script):
    """Load and run scripts from this repo's ``examples/`` dir as a user
    would copy and pasing them into their editor.

    On windows a little more "finessing" is done to make
    ``multiprocessing`` play nice: we copy the ``__main__.py`` into the
    test directory and invoke the script as a module with ``python -m
    test_example``.
    """
    ex_file = os.path.join(*example_script)

    if 'rpc_bidir_streaming' in ex_file and sys.version_info < (3, 9):
        pytest.skip("2-way streaming example requires py3.9 async with syntax")

    with open(ex_file, 'r') as ex:
        code = ex.read()

        with run_example_in_subproc(code) as proc:
            proc.wait()
            err, _ = proc.stderr.read(), proc.stdout.read()
            # print(f'STDERR: {err}')
            # print(f'STDOUT: {out}')

            # if we get some gnarly output let's aggregate and raise
            if err:
                errmsg = err.decode()
                errlines = errmsg.splitlines()
                last_error = errlines[-1]
                if (
                    'Error' in last_error

                    # XXX: currently we print this to console, but maybe
                    # shouldn't eventually once we figure out what's
                    # a better way to be explicit about aio side
                    # cancels?
                    and 'asyncio.exceptions.CancelledError' not in last_error
                ):
                    raise Exception(errmsg)

            assert proc.returncode == 0
