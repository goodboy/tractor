"""
Our classy exception set.
"""
import builtins
import traceback


class RemoteActorError(Exception):
    # TODO: local recontruction of remote exception deats
    "Remote actor exception bundled locally"
    def __init__(self, message, type_str, **msgdata):
        super().__init__(message)
        self.type = getattr(builtins, type_str, Exception)
        self.msgdata = msgdata

    # TODO: a trio.MultiError.catch like context manager
    # for catching underlying remote errors of a particular type


class InternalActorError(RemoteActorError):
    """Remote internal ``tractor`` error indicating
    failure of some primitive or machinery.
    """


class NoResult(RuntimeError):
    "No final result is expected for this actor"


def pack_error(exc):
    """Create an "error message" for tranmission over
    a channel (aka the wire).
    """
    return {
        'error': {
            'tb_str': traceback.format_exc(),
            'type_str': type(exc).__name__,
        }
    }


def unpack_error(msg, chan=None):
    """Unpack an 'error' message from the wire
    into a local ``RemoteActorError``.
    """
    tb_str = msg['error'].get('tb_str', '')
    return RemoteActorError(
        f"{chan.uid}\n" + tb_str,
        **msg['error'],
    )
