# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

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

"""
CLI framework extensions for hacking on the actor runtime.

Currently popular frameworks supported are:

  - `typer` via the `@callback` API

"""
from __future__ import annotations
from typing import (
    Any,
    Callable,
)
from typing_extensions import Annotated

import typer


_runtime_vars: dict[str, Any] = {}


def load_runtime_vars(
    ctx: typer.Context,
    callback: Callable,
    pdb: bool = False,  # --pdb
    ll: Annotated[
        str,
        typer.Option(
            '--loglevel',
            '-l',
            help='BigD logging level',
        ),
    ] = 'cancel',  # -l info
):
    '''
    Maybe engage crash handling with `pdbp` when code inside
    a `typer` CLI endpoint cmd raises.

    To use this callback simply take your `app = typer.Typer()` instance
    and decorate this function with it like so:

    .. code:: python

        from tractor.devx import cli

        app = typer.Typer()

        # manual decoration to hook into `click`'s context system!
        cli.load_runtime_vars = app.callback(
            invoke_without_command=True,
        )

    And then you can use the now augmented `click` CLI context as so,

    .. code:: python

        @app.command(
            context_settings={
                "allow_extra_args": True,
                "ignore_unknown_options": True,
            }
        )
        def my_cli_cmd(
            ctx: typer.Context,
        ):
            rtvars: dict = ctx.runtime_vars
            pdb: bool = rtvars['pdb']

            with tractor.devx.cli.maybe_open_crash_handler(pdb=pdb):
                trio.run(
                    partial(
                        my_tractor_main_task_func,
                        debug_mode=pdb,
                        loglevel=rtvars['ll'],
                    )
                )

    which will enable log level and debug mode globally for the entire
    `tractor` + `trio` runtime thereafter!

    Bo

    '''
    global _runtime_vars
    _runtime_vars |= {
        'pdb': pdb,
        'll': ll,
    }

    ctx.runtime_vars: dict[str, Any] = _runtime_vars
    print(
        f'`typer` sub-cmd: {ctx.invoked_subcommand}\n'
        f'`tractor` runtime vars: {_runtime_vars}'
    )

    # XXX NOTE XXX: hackzone.. if no sub-cmd is specified (the
    # default if the user just invokes `bigd`) then we simply
    # invoke the sole `_bigd()` cmd passing in the "parent"
    # typer.Context directly to that call since we're treating it
    # as a "non sub-command" or wtv..
    # TODO: ideally typer would have some kinda built-in way to get
    # this behaviour without having to construct and manually
    # invoke our own cmd..
    if (
        ctx.invoked_subcommand is None
        or ctx.invoked_subcommand == callback.__name__
    ):
        cmd: typer.core.TyperCommand = typer.core.TyperCommand(
            name='bigd',
            callback=callback,
        )
        ctx.params = {'ctx': ctx}
        cmd.invoke(ctx)
