"""
This is near-copy of the 3.8 stdlib's ``multiprocessing.forkserver.py``
with some hackery to prevent any more then a single forkserver and
semaphore tracker per ``MainProcess``.
"""
import os
import socket
import signal
import struct
import sys
import errno
import selectors
import warnings

from multiprocessing import (
    forkserver, semaphore_tracker, spawn, process, util
)
from multiprocessing.forkserver import (
        ForkServer, MAXFDS_TO_SEND
        # _serve_one,
)
from multiprocessing.context import reduction


# taken from 3.8
SIGNED_STRUCT = struct.Struct('q')     # large enough for pid_t


class PatchedForkServer(ForkServer):

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
                      semaphore_tracker.getfd()]
            allfds += fds

            # This is the only part changed
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
            # This is the only part changed

                return parent_r, parent_w
            except Exception:
                os.close(parent_r)
                os.close(parent_w)
                raise
            finally:
                os.close(child_r)
                os.close(child_w)


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
                            warnings.warn('forkserver: waitpid returned '
                                          'unexpected pid %d' % pid)

                if listener in rfds:
                    # Incoming fork request
                    with listener.accept()[0] as s:

                        # Thing changed - be tolerant of socket disconnects
                        try:
                            # Receive fds from client
                            fds = reduction.recvfds(s, MAXFDS_TO_SEND + 1)
                        except EOFError:
                            # broken socket due to reconnection on client-side
                            continue
                        # Thing changed - be tolerant of socket disconnects

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

    (_forkserver._forkserver_alive_fd,
     semaphore_tracker._semaphore_tracker._fd,
     *_forkserver._inherited_fds) = fds

    # Run process object received over pipe
    code = spawn._main(child_r)

    return code


def write_signed(fd, n):
    msg = SIGNED_STRUCT.pack(n)
    while msg:
        nbytes = os.write(fd, msg)
        if nbytes == 0:
            raise RuntimeError('should not get here')
        msg = msg[nbytes:]


class PatchedSemaphoreTracker(semaphore_tracker.SemaphoreTracker):
    """Stop GD ensuring everything is running...
    """
    def getfd(self):
        # self.ensure_running()
        return self._fd


_semaphore_tracker = PatchedSemaphoreTracker()
_forkserver = PatchedForkServer()


def override_stdlib():
    """Override the stdlib's ``multiprocessing.forkserver`` behaviour
    such that our local "manually managed" version from above can be
    used instead.

    This allows avoiding spawning superfluous additional forkservers
    and semaphore trackers for each actor nursery used to create new
    sub-actors from sub-actors.
    """
    semaphore_tracker._semaphore_tracker = _semaphore_tracker
    semaphore_tracker.ensure_running = _semaphore_tracker.ensure_running
    semaphore_tracker.register = _semaphore_tracker.register
    semaphore_tracker.unregister = _semaphore_tracker.unregister
    semaphore_tracker.getfd = _semaphore_tracker.getfd

    forkserver._forkserver = _forkserver
    forkserver.main = main
    forkserver._serve_one = _serve_one
    forkserver.ensure_running = _forkserver.ensure_running
    forkserver.get_inherited_fds = _forkserver.get_inherited_fds
    forkserver.connect_to_new_process = _forkserver.connect_to_new_process
    forkserver.set_forkserver_preload = _forkserver.set_forkserver_preload
