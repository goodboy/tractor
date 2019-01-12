"""
Our classy exception set.
"""
import importlib
import builtins
import traceback


_this_mod = importlib.import_module(__name__)


class RemoteActorError(Exception):
    # TODO: local recontruction of remote exception deats
    "Remote actor exception bundled locally"
    def __init__(self, message, type_str, **msgdata):
        super().__init__(message)
        for ns in [builtins, _this_mod]:
            try:
                self.type = getattr(ns, type_str)
                break
            except AttributeError:
                continue
        else:
            self.type = Exception

        self.msgdata = msgdata

    # TODO: a trio.MultiError.catch like context manager
    # for catching underlying remote errors of a particular type


class InternalActorError(RemoteActorError):
    """Remote internal ``tractor`` error indicating
    failure of some primitive or machinery.
    """


class NoResult(RuntimeError):
    "No final result is expected for this actor"


class ModuleNotExposed(ModuleNotFoundError):
    "The requested module is not exposed for RPC"


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


def unpack_error(msg, chan=None, err_type=RemoteActorError):
    """Unpack an 'error' message from the wire
    into a local ``RemoteActorError``.
    """
    tb_str = msg['error'].get('tb_str', '')
    return err_type(
        f"{chan.uid}\n" + tb_str,
        **msg['error'],
    )
