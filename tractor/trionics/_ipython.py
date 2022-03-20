# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet and Mike Nerone.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Example of running an embedded IPython shell inside an already-running
trio loop with working autoawait (it's handy to be able to start an
interactive REPL with your application environment fully initialized).
This is a full solution that works around
https://github.com/ipython/ipython/issues/680 (see
https://gist.github.com/mikenerone/3640fdd450b4ca55ee8df4d4da5a7165 for
how simple it *could* be). This bug should be fixed IMO (using atexit is
a questionable design choice in the first place given the embedding
feature of IPython IMO). As it is now, the entire IPythonAtExitContext
context manager exists only to work around the problem, otherwise it
would result in an error on process exit when IPython's
atexit-registered method calls fail to save the input history. Note: You
may wonder "Why not simply execute and unregister IPython's atexit
registrations?" The answer is that they are bound methods, which can't
be unregistered because you can't get a reference to the registered
bound method (referencing the method again gives you a *new* instance of
a bound method every time).

This code is credited to @mikenerone:matrix.org who put in all the hard
work to get this integration intially up and working. Further adjustments
to get blocking semantics on the ``await tractor.ipython_embed()`` call
were added to the original gist:
https://gist.github.com/mikenerone/786ce75cf8d906ae4ad1e0b57933c23f

'''
import sys
from typing import Any
from unittest.mock import patch

import trio


def trio_embedded_runner(coro):
    return trio.from_thread.run(lambda: coro)


def ipython_worker(
    ns: dict[str, Any],
    ipy_done: trio.Event,
):
    import IPython
    with IPythonAtExitContext():
        IPython.embed(using=trio_embedded_runner, user_ns=ns)

    ipy_done.set()


# TODO: get this shit workin, usage would be something like:
# from .._ipython import ipython
# await ipython(ns=locals())


async def ipython_embed(
    ns: dict[str, Any] = {},
    nonblocking: bool = False,
):
    # we don't require it to be installed.
    import IPython

    # print("In trio loop")

    # TODO: pass in the user's default config...
    # from IPython.config.loader import Config
    # cfg = Config()
    # cfg.InteractiveShellEmbed.prompt_in1="myprompt [\\#]> "
    # cfg.InteractiveShellEmbed.prompt_out="myprompt [\\#]: "
    # cfg.InteractiveShellEmbed.profile=ipythonprofile
    # directly open the shell
    # IPython.embed(config=cfg, user_ns=namespace, banner2=banner)

    # or get shell object and open it later
    # from IPython.frontend.terminal.embed import InteractiveShellEmbed
    # shell = InteractiveShellEmbed(
    #     config=cfg,
    #     user_ns=namespace,
    #     banner2=banner,
    # )
    # shell.user_ns = locals()
    # shell()

    if not ns:
        ns = sys._getframe(1).f_locals

    ipy_done = trio.Event()
    await trio.to_thread.run_sync(
        ipython_worker,
        ns,
        ipy_done,
    )
    if not nonblocking:
        await ipy_done.wait()

    # print("Exiting trio loop")


class IPythonAtExitContext:

    ipython_modules_with_atexit = [
        "IPython.core.magics.script",
        "IPython.core.application",
        "IPython.core.interactiveshell",
        "IPython.core.history",
        "IPython.utils.io",
    ]

    def __init__(self):
        self._calls = []
        self._patchers = []

    def __enter__(self):
        for module in self.ipython_modules_with_atexit:
            try:
                patcher = patch(module + ".atexit", self)
                patcher.start()
            except (AttributeError, ModuleNotFoundError):
                pass
            else:
                self._patchers.append(patcher)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for patcher in self._patchers:
            patcher.stop()
        self._patchers.clear()
        cb_exc = None
        for func, args, kwargs in self._calls:
            # noinspection PyBroadException
            try:
                func(*args, **kwargs)
            except Exception as _exc:
                cb_exc = _exc
        self._calls.clear()
        if cb_exc and not exc_type:
            raise cb_exc

    def register(self, func, *args, **kwargs):
        self._calls.append((func, args, kwargs))

    def unregister(self, func):
        self._calls = [call for call in self._calls if call[0] != func]
