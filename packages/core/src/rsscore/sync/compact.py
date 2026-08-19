"""Compactación del diario de cambios.

Con archivo permanente el `change_log` crece para siempre: cada «marcar como
leído» es una fila más. Como solo importa el último valor de cada campo, las
operaciones antiguas se pueden colapsar... pero nunca por encima del cursor del
cliente más rezagado, o ese cliente perdería cambios que aún no ha visto.
"""

from __future__ import annotations

import sqlite3

__all__ = ["changelog_stats", "compact_change_log", "min_client_seq"]

# Un cliente que lleva meses sin aparecer no puede bloquear la compactación para
# siempre; pasado este plazo se le considera caducado y tendrá que rearrancar
# con un snapshot.
CLIENTE_CADUCADO_MS = 90 * 86_400_000


def min_client_seq(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Cursor del cliente más atrasado. Es el límite duro de la compactación."""
    from ..ids import now_ms

    ahora = now or now_ms()
    fila = conn.execute(
        "SELECT MIN(last_seq) AS s FROM sync_clients WHERE last_seen_at >= ?",
        (ahora - CLIENTE_CADUCADO_MS,),
    ).fetchone()
    if fila is None or fila["s"] is None:
        # Sin clientes vivos, el límite es el final del diario: se puede colapsar todo.
        return conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM change_log").fetchone()["s"]
    return int(fila["s"])


def compact_change_log(
    conn: sqlite3.Connection, *, keep_seq: int, older_than_ms: int | None = None
) -> int:
    """Deja solo la última operación de cada `(entidad, id, campo)` por debajo de
    `keep_seq`. Devuelve cuántas filas se eliminaron."""
    if keep_seq <= 0:
        return 0

    params: list = [keep_seq, keep_seq]
    filtro_fecha = ""
    if older_than_ms is not None:
        filtro_fecha = " AND ts < ?"
        params = [keep_seq, older_than_ms, keep_seq, older_than_ms]

    borradas = conn.execute(
        f"""
        DELETE FROM change_log
        WHERE seq <= ?{filtro_fecha}
          AND seq NOT IN (
              SELECT MAX(seq) FROM change_log
              WHERE seq <= ?{filtro_fecha}
              GROUP BY entity, entity_id, field
          )
        """,
        params,
    ).rowcount

    if borradas:
        conn.execute("PRAGMA optimize")  # nunca VACUUM: bloquea la base entera
    return int(borradas)


def changelog_stats(conn: sqlite3.Connection) -> dict:
    fila = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(MIN(seq), 0) AS lo, COALESCE(MAX(seq), 0) AS hi "
        "FROM change_log"
    ).fetchone()
    unicos = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM change_log GROUP BY entity, entity_id, field)"
    ).fetchone()["n"]
    return {
        "operaciones": fila["n"],
        "campos_unicos": unicos,
        "cursor_min": fila["lo"],
        "cursor_max": fila["hi"],
        "compactable": max(fila["n"] - unicos, 0),
    }
