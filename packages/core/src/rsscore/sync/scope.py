"""Replicación parcial.

El archivo es permanente y crece sin límite, así que el móvil no puede replicarlo
entero: declara una ventana (`SyncScope`) y el hub filtra el delta en el servidor,
antes de enviarlo. Las operaciones estructurales —feeds, carpetas, etiquetas,
reglas— pasan siempre: son pocas y sin ellas la interfaz no se puede ni dibujar.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from .. import repo
from ..ids import now_ms
from ..models import ChangeOp, Entity, SyncScope

__all__ = ["entries_in_scope", "filter_ops_for_scope", "is_entry_in_scope"]

# Entidades pequeñas y estructurales: nunca se filtran.
_SIEMPRE = {Entity.FEED, Entity.FOLDER, Entity.TAG, Entity.RULE, Entity.SAVED_SEARCH}


def _scope_feed_ids(conn: sqlite3.Connection, scope: SyncScope) -> set[str] | None:
    """Feeds que entran en el ámbito. `None` significa «todos»."""
    if not scope.feed_ids and not scope.folder_ids:
        return None
    ids = set(scope.feed_ids)
    if scope.folder_ids:
        carpetas = repo.descendant_folder_ids(conn, scope.folder_ids)
        marcas = ",".join("?" * len(carpetas))
        filas = conn.execute(
            f"SELECT id FROM feeds WHERE deleted = 0 AND folder_id IN ({marcas})", carpetas
        ).fetchall()
        ids.update(r["id"] for r in filas)
    return ids


def is_entry_in_scope(
    conn: sqlite3.Connection,
    entry_id: str,
    scope: SyncScope,
    *,
    feed_ids: set[str] | None = None,
    now: int | None = None,
) -> bool:
    fila = conn.execute(
        "SELECT e.feed_id, e.published_at, s.read, s.starred FROM entries e "
        "LEFT JOIN entry_state s ON s.entry_id = e.id WHERE e.id = ?",
        (entry_id,),
    ).fetchone()
    if fila is None:
        # Si aquí no conocemos la entrada no podemos decidir; dejamos pasar la
        # operación y que el receptor la aparque si tampoco la tiene.
        return True

    if feed_ids is not None and fila["feed_id"] not in feed_ids:
        return False
    if scope.include_starred and fila["starred"]:
        return True
    if scope.include_unread and not fila["read"]:
        return True
    if scope.days is None:
        return True
    limite = (now or now_ms()) - scope.days * 86_400_000
    return fila["published_at"] >= limite


def filter_ops_for_scope(
    conn: sqlite3.Connection, ops: Sequence[ChangeOp], scope: SyncScope
) -> list[ChangeOp]:
    """Descarta las operaciones de artículos que el cliente no replica."""
    feed_ids = _scope_feed_ids(conn, scope)
    ahora = now_ms()
    cache: dict[str, bool] = {}
    salida: list[ChangeOp] = []

    for op in ops:
        if op.entity in _SIEMPRE:
            salida.append(op)
            continue
        entry_id = op.entity_id.partition(":")[0]
        dentro = cache.get(entry_id)
        if dentro is None:
            dentro = is_entry_in_scope(conn, entry_id, scope, feed_ids=feed_ids, now=ahora)
            cache[entry_id] = dentro
        if dentro:
            salida.append(op)
    return salida


def entries_in_scope(
    conn: sqlite3.Connection,
    scope: SyncScope,
    *,
    since: int | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[str]:
    """Ids de las entradas que el cliente debe replicar, más recientes primero."""
    where = ["1=1"]
    params: list = []

    feed_ids = _scope_feed_ids(conn, scope)
    if feed_ids is not None:
        if not feed_ids:
            return []
        where.append(f"e.feed_id IN ({','.join('?' * len(feed_ids))})")
        params += sorted(feed_ids)

    ventana: list[str] = []
    if scope.days is not None:
        ventana.append("e.published_at >= ?")
        params.append((since or now_ms()) - scope.days * 86_400_000)
    if scope.include_starred:
        ventana.append("s.starred = 1")
    if scope.include_unread:
        ventana.append("s.read = 0")
    if ventana:
        where.append("(" + " OR ".join(ventana) + ")")

    sql = (
        "SELECT e.id FROM entries e LEFT JOIN entry_state s ON s.entry_id = e.id "
        f"WHERE {' AND '.join(where)} ORDER BY e.published_at DESC LIMIT ? OFFSET ?"
    )
    params += [min(limit or scope.max_entries, scope.max_entries), offset]
    return [r["id"] for r in conn.execute(sql, params)]
