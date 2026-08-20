"""Sincronización offline-first entre el hub y los clientes.

El modelo es un diario de cambios con reloj de Lamport: cada escritura local
genera una operación `(entidad, id, campo, valor, lamport, device_id)`; los
clientes suben su cola y bajan el delta desde un cursor. Los conflictos se
resuelven campo a campo con last-write-wins, desempatando por `device_id` para
que el resultado sea idéntico en todos los nodos sin necesidad de coordinación.
"""

from .apply import ApplyResult, apply_ops, replay_pending
from .compact import compact_change_log, min_client_seq
from .scope import entries_in_scope, filter_ops_for_scope, is_entry_in_scope
from .snapshot import apply_snapshot, build_snapshot, iter_snapshot_chunks

__all__ = [
    "ApplyResult",
    "apply_ops",
    "apply_snapshot",
    "build_snapshot",
    "compact_change_log",
    "entries_in_scope",
    "filter_ops_for_scope",
    "is_entry_in_scope",
    "iter_snapshot_chunks",
    "min_client_seq",
    "replay_pending",
]


def __getattr__(name: str):
    """`SyncClient` necesita httpx; se importa solo si se pide."""
    if name in {"SyncClient", "SyncStats"}:
        from . import client

        return getattr(client, name)
    raise AttributeError(name)
