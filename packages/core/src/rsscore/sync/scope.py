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
    previo: tuple[str, bool] | None = None,
) -> bool:
    """¿Replica el cliente esta entrada?

    `previo` permite preguntarlo por el estado ANTERIOR a una operación, en la
    forma `(campo, valor)`. Hace falta para las operaciones que sacan la entrada
    del ámbito: ver `filter_ops_for_scope`.
    """
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

    leido, guardado = fila["read"], fila["starred"]
    if previo is not None:
        campo, valor = previo
        if campo == "read":
            leido = valor
        elif campo == "starred":
            guardado = valor

    if scope.include_starred and guardado:
        return True
    if scope.include_unread and not leido:
        return True
    if scope.days is None:
        return True
    limite = (now if now is not None else now_ms()) - scope.days * 86_400_000
    return fila["published_at"] >= limite


def filter_ops_for_scope(
    conn: sqlite3.Connection, ops: Sequence[ChangeOp], scope: SyncScope
) -> list[ChangeOp]:
    """Descarta las operaciones de artículos que el cliente no replica.

    El ámbito se mira sobre el estado que la entrada tiene AHORA, es decir ya con
    la operación aplicada, y eso deja fuera justo a las operaciones que sacan la
    entrada del ámbito: marcar como leído un artículo más viejo que la ventana lo
    saca de `include_unread` y de la ventana a la vez, así que la operación que lo
    marcó se descartaría y el cliente lo tendría sin leer para siempre. Por eso
    una operación de estado se entrega si la entrada está en el ámbito con el
    estado actual O con el anterior a esa misma operación.
    """
    feed_ids = _scope_feed_ids(conn, scope)
    ahora = now_ms()
    # La clave lleva el valor además del campo: en un mismo lote pueden venir
    # `starred=true` y `starred=false` de la misma entrada, y preguntan cosas
    # distintas.
    cache: dict[tuple[str, tuple[str, bool] | None], bool] = {}
    salida: list[ChangeOp] = []

    def dentro(entry_id: str, previo: tuple[str, bool] | None) -> bool:
        clave = (entry_id, previo)
        valor = cache.get(clave)
        if valor is None:
            valor = is_entry_in_scope(
                conn, entry_id, scope, feed_ids=feed_ids, now=ahora, previo=previo
            )
            cache[clave] = valor
        return valor

    for op in ops:
        if op.entity in _SIEMPRE:
            salida.append(op)
            continue

        entry_id = op.entity_id.partition(":")[0]
        if dentro(entry_id, None):
            salida.append(op)
            continue

        # Los dos campos que ensanchan el ámbito son booleanos, así que el valor
        # anterior es la negación del que trae la operación.
        if (
            op.entity is Entity.ENTRY_STATE
            and op.field in ("read", "starred")
            and dentro(entry_id, (op.field, not bool(op.value)))
        ):
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
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("El límite y el desplazamiento no pueden ser negativos")
    restantes = max(0, scope.max_entries - offset)
    cantidad = min(limit if limit is not None else scope.max_entries, restantes)
    if cantidad <= 0:
        return []
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
        params.append((since if since is not None else now_ms()) - scope.days * 86_400_000)
        if scope.include_starred:
            ventana.append("s.starred = 1")
        if scope.include_unread:
            ventana.append("COALESCE(s.read, 0) = 0")
    if ventana:
        where.append("(" + " OR ".join(ventana) + ")")

    sql = (
        "SELECT e.id FROM entries e LEFT JOIN entry_state s ON s.entry_id = e.id "
        f"WHERE {' AND '.join(where)} ORDER BY e.published_at DESC, e.id LIMIT ? OFFSET ?"
    )
    params += [cantidad, offset]
    return [r["id"] for r in conn.execute(sql, params)]
