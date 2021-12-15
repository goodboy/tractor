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
This is near-copy of the 3.8 stdlib's ``multiprocessing.forkserver.py``
with some hackery to prevent any more then a single forkserver and
semaphore tracker per ``MainProcess``.

.. note:: There is no type hinting in this code base (yet) to remain as
          a close as possible to upstream.
"""
# type: ignore

import os
import socket
import signal
import struct
import sys
import errno
import selectors
import warnings

try:
    from multiprocessing import semaphore_tracker  # type: ignore
    resource_tracker = semaphore_tracker
    resource_tracker._resource_tracker = resource_tracker._semaphore_tracker
    _tracker_type = resource_tracker.SemaphoreTracker
except ImportError:
    # 3.8 introduces a more general version that also tracks shared mems
    from multiprocessing import resource_tracker  # type: ignore
    _tracker_type = resource_tracker.ResourceTracker

from multiprocessing import spawn, process  # type: ignore
from multiprocessing import forkserver, util, connection  # type: ignore
from multiprocessing.forkserver import (
        ForkServer, MAXFDS_TO_SEND
)
from multiprocessing.context import reduction  # type: ignore


# taken from 3.8
SIGNED_STRUCT = struct.Struct('q')     # large enough for pid_t


class PatchedForkServer(ForkServer):

    _forkserver_pid = None

    def connect_to_new_process(self, fds):
        '''Request forkserver to create a child process.

        Returns a pair of fds (status_r, data_w).  The calling process can read
        the child process's pid and (eventually) its returncode from status_r.
        The calling process should write to data_w the pickled preparation and
        process data.
        '''
        # self.ensure_running()  # treat our users like adults expecting
        # them to spawn the server on their own
        if len(fds) + 4 >= MAXFDS_TO_SEND:
            raise ValueError('too many fds')
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(self._forkserver_address)
            parent_r, child_w = os.pipe()
            child_r, parent_w = os.pipe()
            allfds = [child_r, child_w, self._forkserver_alive_fd,
                      resource_tracker.getfd()]
            allfds += fds

            # XXX This is the only part changed
            try:
                while True:
                    try:
                        reduction.sendfds(client, allfds)
                        break
                    except OSError as err:
                        if err.errno == errno.EBADF:
                            print(f"Bad FD {err}")
                            client = socket.socket(socket.AF_UNIX)
                            client.connect(self._forkserver_address)
                            continue
                        raise
            # XXX This is the only part changed

                return parent_r, parent_w
            except Exception:
                os.close(parent_r)
                os.close(parent_w)
                raise
            finally:
                os.close(child_r)
                os.close(child_w)

    def ensure_running(self):
        '''Make sure that a fork server is running.

        This can be called from any process.  Note that usually a child
        process will just reuse the forkserver started by its parent, so
        ensure_running() will do nothing.
        '''
        with self._lock:
            resource_tracker.ensure_running()
            if self._forkserver_pid is not None:
                # forkserver was launched before, is it still running?
                pid, status = os.waitpid(self._forkserver_pid, os.WNOHANG)
                if not pid:
                    # still alive
                    return
                # dead, launch it again
                os.close(self._forkserver_alive_fd)
                self._forkserver_address = None
                self._forkserver_alive_fd = None
                self._forkserver_pid = None

            # XXX only thing that changed!
            cmd = ('from tractor._forkserver_override import main; ' +
                   'main(%d, %d, %r, **%r)')

            if self._preload_modules:
                desired_keys = {'main_path', 'sys_path'}
                data = spawn.get_preparation_data('ignore')
                data = {x: y for x, y in data.items() if x in desired_keys}
            else:
                data = {}

            with socket.socket(socket.AF_UNIX) as listener:
                address = connection.arbitrary_address('AF_UNIX')
                listener.bind(address)
                if not util.is_abstract_socket_namespace(address):
                    os.chmod(address, 0o600)
                listener.listen()

                # all client processes own the write end of the "alive" pipe;
                # when they all terminate the read end becomes ready.
                alive_r, alive_w = os.pipe()
                try:
                    fds_to_pass = [listener.fileno(), alive_r]
                    cmd %= (listener.fileno(), alive_r, self._preload_modules,
                            data)
                    exe = spawn.get_executable()
                    args = [exe] + util._args_from_interpreter_flags()
                    args += ['-c', cmd]
                    pid = util.spawnv_passfds(exe, args, fds_to_pass)
                except:
                    os.close(alive_w)
                    raise
                finally:
                    os.close(alive_r)
                self._forkserver_address = address
                self._forkserver_alive_fd = alive_w
                self._forkserver_pid = pid


def main(listener_fd, alive_r, preload, main_path=None, sys_path=None):
    '''Run forkserver.'''
    if preload:
        if '__main__' in preload and main_path is not None:
            process.current_process()._inheriting = True
            try:
                spawn.import_main_path(main_path)
            finally:
                del process.current_process()._inheriting
        for modname in preload:
            try:
                __import__(modname)
            except ImportError:
                pass

    util._close_stdin()

    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_r, False)
    os.set_blocking(sig_w, False)

    def sigchld_handler(*_unused):
        # Dummy signal handler, doesn't do anything
        pass

    handlers = {
        # unblocking SIGCHLD allows the wakeup fd to notify our event loop
        signal.SIGCHLD: sigchld_handler,
        # protect the process from ^C
        signal.SIGINT: signal.SIG_IGN,
        }
    old_handlers = {sig: signal.signal(sig, val)
                    for (sig, val) in handlers.items()}

    # calling os.write() in the Python signal handler is racy
    signal.set_wakeup_fd(sig_w)

    # map child pids to client fds
    pid_to_fd = {}

    with socket.socket(socket.AF_UNIX, fileno=listener_fd) as listener, \
         selectors.DefaultSelector() as selector:
        _forkserver._forkserver_address = listener.getsockname()

        selector.register(listener, selectors.EVENT_READ)
        selector.register(alive_r, selectors.EVENT_READ)
        selector.register(sig_r, selectors.EVENT_READ)

        while True:
            try:
                while True:
                    rfds = [key.fileobj for (key, events) in selector.select()]
                    if rfds:
                        break

                if alive_r in rfds:
                    # EOF because no more client processes left
                    assert os.read(alive_r, 1) == b'', "Not at EOF?"
                    raise SystemExit

                if sig_r in rfds:
                    # Got SIGCHLD
                    os.read(sig_r, 65536)  # exhaust
                    while True:
                        # Scan for child processes
                        try:
                            pid, sts = os.waitpid(-1, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if pid == 0:
                            break
                        child_w = pid_to_fd.pop(pid, None)
                        if child_w is not None:
                            if os.WIFSIGNALED(sts):
                                returncode = -os.WTERMSIG(sts)
                            else:
                                if not os.WIFEXITED(sts):
                                    raise AssertionError(
                                        "Child {0:n} status is {1:n}".format(
                                            pid, sts))
                                returncode = os.WEXITSTATUS(sts)
                            # Send exit code to client process
                            try:
                                write_signed(child_w, returncode)
                            except BrokenPipeError:
                                # client vanished
                                pass
                            os.close(child_w)
                        else:
                            # This shouldn't happen really
                            warnings.warning('forkserver: waitpid returned '
                                             'unexpected pid %d' % pid)

                if listener in rfds:
                    # Incoming fork request
                    with listener.accept()[0] as s:

                        # XXX Thing that changed - be tolerant of socket disconnects
                        try:
                            # Receive fds from client
                            fds = reduction.recvfds(s, MAXFDS_TO_SEND + 1)
                        except EOFError:
                            # broken socket due to reconnection on client-side
                            continue
                        # XXX Thing that changed - be tolerant of socket disconnects

                        if len(fds) > MAXFDS_TO_SEND:
                            raise RuntimeError(
                                "Too many ({0:n}) fds to send".format(
                                    len(fds)))
                        child_r, child_w, *fds = fds
                        s.close()
                        pid = os.fork()
                        if pid == 0:
                            # Child
                            code = 1
                            try:
                                listener.close()
                                selector.close()
                                unused_fds = [alive_r, child_w, sig_r, sig_w]
                                unused_fds.extend(pid_to_fd.values())
                                code = _serve_one(child_r, fds,
                                                  unused_fds,
                                                  old_handlers)
                            except Exception:
                                sys.excepthook(*sys.exc_info())
                                sys.stderr.flush()
                            finally:
                                os._exit(code)
                        else:
                            # Send pid to client process
                            try:
                                write_signed(child_w, pid)
                            except BrokenPipeError:
                                # client vanished
                                pass
                            pid_to_fd[pid] = child_w
                            os.close(child_r)
                            for fd in fds:
                                os.close(fd)

            except OSError as e:
                if e.errno != errno.ECONNABORTED:
                    raise


def _serve_one(child_r, fds, unused_fds, handlers):
    # close unnecessary stuff and reset signal handlers
    signal.set_wakeup_fd(-1)
    for sig, val in handlers.items():
        signal.signal(sig, val)
    for fd in unused_fds:
        os.close(fd)

    # XXX this assigment is why we override this func...
    (_forkserver._forkserver_alive_fd,
     resource_tracker._resource_tracker._fd,
     *_forkserver._inherited_fds) = fds

    # Run process object received over pipe
    # XXX this clearly changed
    try:
        # spawn._main() signature changed in 3.8
        parent_sentinel = os.dup(child_r)
        code = spawn._main(child_r, parent_sentinel)
    except TypeError:
        # pre 3.8 signature
        code = spawn._main(child_r)

    return code


def write_signed(fd, n):
    msg = SIGNED_STRUCT.pack(n)
    while msg:
        nbytes = os.write(fd, msg)
        if nbytes == 0:
            raise RuntimeError('should not get here')
        msg = msg[nbytes:]


class PatchedResourceTracker(_tracker_type):  # type: ignore
    """Stop GD ensuring everything is running...
    """
    def getfd(self):
        # XXX: why must be constantly spawn trackers..
        # self.ensure_running()
        return self._fd


_resource_tracker = PatchedResourceTracker()
_forkserver = PatchedForkServer()


def override_stdlib():
    """Override the stdlib's ``multiprocessing.forkserver`` behaviour
    such that our local "manually managed" version from above can be
    used instead.

    This allows avoiding spawning superfluous additional forkservers
    and semaphore trackers for each actor nursery used to create new
    sub-actors from sub-actors.
    """
    try:
        resource_tracker._semaphore_tracker = _resource_tracker
        resource_tracker._resource_tracker = _resource_tracker
    except AttributeError:
        resource_tracker._resource_tracker = _resource_tracker

    resource_tracker.ensure_running = _resource_tracker.ensure_running
    resource_tracker.register = _resource_tracker.register
    resource_tracker.unregister = _resource_tracker.unregister
    resource_tracker.getfd = _resource_tracker.getfd

    forkserver._forkserver = _forkserver
    forkserver.ensure_running = _forkserver.ensure_running
    forkserver.main = main
    forkserver._serve_one = _serve_one
    forkserver.get_inherited_fds = _forkserver.get_inherited_fds
    forkserver.connect_to_new_process = _forkserver.connect_to_new_process
    forkserver.set_forkserver_preload = _forkserver.set_forkserver_preload
