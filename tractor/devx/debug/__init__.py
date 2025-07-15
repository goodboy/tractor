# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.

'''
Multi-actor debugging for da peeps!

'''
from __future__ import annotations
from tractor.log import get_logger
from ._repl import (
    PdbREPL as PdbREPL,
    mk_pdb as mk_pdb,
    TractorConfig as TractorConfig,
)
from ._tty_lock import (
    DebugStatus as DebugStatus,
    DebugStateError as DebugStateError,
)
from ._trace import (
    Lock as Lock,
    _pause_msg as _pause_msg,
    _repl_fail_msg as _repl_fail_msg,
    _set_trace as _set_trace,
    _sync_pause_from_builtin as _sync_pause_from_builtin,
    breakpoint as breakpoint,
    maybe_init_greenback as maybe_init_greenback,
    maybe_import_greenback as maybe_import_greenback,
    pause as pause,
    pause_from_sync as pause_from_sync,
)
from ._post_mortem import (
    BoxedMaybeException as BoxedMaybeException,
    maybe_open_crash_handler as maybe_open_crash_handler,
    open_crash_handler as open_crash_handler,
    post_mortem as post_mortem,
    _crash_msg as _crash_msg,
    _maybe_enter_pm as _maybe_enter_pm,
)
from ._sync import (
    maybe_wait_for_debugger as maybe_wait_for_debugger,
    acquire_debug_lock as acquire_debug_lock,
)
from ._sigint import (
    sigint_shield as sigint_shield,
    _ctlc_ignore_header as _ctlc_ignore_header
)

log = get_logger(__name__)

# ----------------
# XXX PKG TODO XXX
# ----------------
# refine the internal impl and APIs!
#
# -[ ] rework `._pause()` and it's branch-cases for root vs.
#     subactor:
#  -[ ] `._pause_from_root()` + `_pause_from_subactor()`?
#  -[ ]  do the de-factor based on bg-thread usage in
#    `.pause_from_sync()` & `_pause_from_bg_root_thread()`.
#  -[ ] drop `debug_func == None` case which is confusing af..
#  -[ ]  factor out `_enter_repl_sync()` into a util func for calling
#    the `_set_trace()` / `_post_mortem()` APIs?
#
# -[ ] figure out if we need `acquire_debug_lock()` and/or re-implement
#    it as part of the `.pause_from_sync()` rework per above?
#
# -[ ] pair the `._pause_from_subactor()` impl with a "debug nursery"
#   that's dynamically allocated inside the `._rpc` task thus
#   avoiding the `._service_n.start()` usage for the IPC request?
#  -[ ] see the TODO inside `._rpc._errors_relayed_via_ipc()`
#
# -[ ] impl a `open_debug_request()` which encaps all
#   `request_root_stdio_lock()` task scheduling deats
#   + `DebugStatus` state mgmt; which should prolly be re-branded as
#   a `DebugRequest` type anyway AND with suppoort for bg-thread
#   (from root actor) usage?
#
# -[ ] handle the `xonsh` case for bg-root-threads in the SIGINT
#     handler!
#   -[ ] do we need to do the same for subactors?
#   -[ ] make the failing tests finally pass XD
#
# -[ ] simplify `maybe_wait_for_debugger()` to be a root-task only
#     API?
#   -[ ] currently it's implemented as that so might as well make it
#     formal?
