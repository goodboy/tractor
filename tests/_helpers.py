'''
Shared helpers for actor-runtime test suites.

'''
from pathlib import Path
from types import TracebackType

import tractor


class CancellationMarkers:
    '''
    Mark a test endpoint's lifetime without cleanup checkpoints.

    '''
    def __init__(
        self,
        started_path: str,
        cancelled_path: str,
    ) -> None:
        self.started_path = started_path
        self.cancelled_path = cancelled_path

    def __enter__(self) -> None:
        Path(self.started_path).touch()

    def __exit__(
        self,
        exc_type: type[BaseException]|None,
        exc_value: BaseException|None,
        traceback: TracebackType|None,
    ) -> None:
        Path(self.cancelled_path).touch()


def non_registration_contexts(
    actor: tractor.Actor,
) -> dict[tuple, str]:
    '''
    Snapshot application contexts without registrar-service traffic.

    '''
    return {
        key: str(ctx._nsf)
        for key, ctx in actor._contexts.items()
        if str(ctx._nsf) != (
            'tractor.discovery._registry:'
            'Registrar.register_actor'
        )
    }
