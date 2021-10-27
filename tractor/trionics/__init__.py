'''
Sugary patterns for trio + tractor designs.

'''
from ._mngrs import gather_contexts
from ._broadcast import broadcast_receiver, BroadcastReceiver, Lagged


__all__ = [
    'gather_contexts',
    'broadcast_receiver',
    'BroadcastReceiver',
    'Lagged',
]
