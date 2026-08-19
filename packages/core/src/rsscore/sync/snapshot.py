"""Arranque de un cliente nuevo.

Reproducir un diario de millones de operaciones para poner al día un móvil recién
instalado no es viable, así que el hub sirve una foto del estado dentro del
ámbito del cliente y el cursor exacto del diario en ese instante. A partir de ahí
el cliente sigue con deltas normales.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from ..ids import now_ms
from ..models import SyncScope
from .scope import entries_in_scope

__all__ = ["apply_snapshot", "build_snapshot", "iter_snapshot_chunks"]

SNAPSHOT_VERSION = 1


def _cursor(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM change_log").fetchone()["s"]


def _structure(conn: sqlite3.Connection) -> dict:
    """Lo pequeño y estructural: siempre va entero, sin filtrar por ámbito."""
    return {
        "folders": [
            dict(r)
            for r in conn.execute(
                "SELECT id, parent_id, name, position, deleted, lamport, device_id, updated_at "
                "FROM folders"
            )
        ],
        "feeds": [
            dict(r)
            for r in conn.execute(
                "SELECT id, folder_id, url, site_url, title, custom_title, description, "
                "icon_url, interval_seconds, fetch_full_text, disabled, source_kind, "
                "source_config_json, deleted, lamport, device_id, updated_at FROM feeds"
            )
        ],
        "tags": [
            dict(r)
            for r in conn.execute("SELECT id, name, color, deleted, lamport, device_id FROM tags")
        ],
        "rules": [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, enabled, position, spec_json, deleted, lamport, device_id, "
                "updated_at FROM rules"
            )
        ],
        "saved_searches": [
            dict(r)
            for r in conn.execute(
                "SELECT id, name, query, filter_json, position, deleted, lamport, device_id "
                "FROM saved_searches"
            )
        ],
    }


def _entries_chunk(conn: sqlite3.Connection, ids: list[str]) -> dict:
    if not ids:
        return {"entries": [], "state": [], "entry_tags": []}
    marcas = ",".join("?" * len(ids))
    entradas = [
        dict(r)
        for r in conn.execute(
            "SELECT id, feed_id, guid_hash, content_hash, url, title, author, summary, "
            f"published_at, updated_at, fetched_at, has_body, enclosure_url, enclosure_type "
            f"FROM entries WHERE id IN ({marcas})",
            ids,
        )
    ]
    estado = [
        dict(r)
        for r in conn.execute(
            f"SELECT entry_id, read, starred, read_at, star_at, lamport, device_id "
            f"FROM entry_state WHERE entry_id IN ({marcas})",
            ids,
        )
    ]
    etiquetas = [
        dict(r)
        for r in conn.execute(
            f"SELECT entry_id, tag_id, deleted, lamport, device_id FROM entry_tags "
            f"WHERE entry_id IN ({marcas})",
            ids,
        )
    ]
    return {"entries": entradas, "state": estado, "entry_tags": etiquetas}


def build_snapshot(conn: sqlite3.Connection, scope: SyncScope) -> dict:
    """Foto completa del ámbito. Sin cuerpos: se piden bajo demanda al abrir."""
    cursor = _cursor(conn)
    ids = entries_in_scope(conn, scope)
    datos = _entries_chunk(conn, ids)
    return {
        "version": SNAPSHOT_VERSION,
        "cursor": cursor,
        "generated_at": now_ms(),
        "scope": scope.model_dump(),
        "server_lamport": conn.execute("SELECT lamport FROM node WHERE id = 1").fetchone()[
            "lamport"
        ],
        **_structure(conn),
        **datos,
    }


def iter_snapshot_chunks(
    conn: sqlite3.Connection, scope: SyncScope, chunk: int = 5000
) -> Iterator[dict]:
    """Igual que `build_snapshot` pero por trozos.

    Con 500 feeds y años de archivo, construir el JSON entero en memoria no es una
    opción; el primer trozo lleva la estructura y los siguientes solo artículos.
    """
    cursor = _cursor(conn)
    yield {
        "version": SNAPSHOT_VERSION,
        "cursor": cursor,
        "generated_at": now_ms(),
        "scope": scope.model_dump(),
        "chunk": 0,
        "final": False,
        **_structure(conn),
        "entries": [],
        "state": [],
        "entry_tags": [],
    }
    offset, indice = 0, 1
    while True:
        ids = entries_in_scope(conn, scope, limit=chunk, offset=offset)
        if not ids:
            break
        datos = _entries_chunk(conn, ids)
        offset += len(ids)
        indice += 1
        yield {
            "version": SNAPSHOT_VERSION,
            "cursor": cursor,
            "chunk": indice,
            "final": False,
            **datos,
        }
    yield {"version": SNAPSHOT_VERSION, "cursor": cursor, "chunk": indice, "final": True}


def apply_snapshot(conn: sqlite3.Connection, snapshot: dict) -> int:
    """Escribe una foto en el cliente y coloca su cursor donde toca."""
    if snapshot.get("version", 1) > SNAPSHOT_VERSION:
        raise ValueError(f"snapshot de versión {snapshot['version']}: actualiza el cliente")
    n = 0
    n += _upsert(conn, "folders", snapshot.get("folders", []), ("id",))
    n += _upsert(conn, "feeds", snapshot.get("feeds", []), ("id",))
    n += _upsert(conn, "tags", snapshot.get("tags", []), ("id",))
    n += _upsert(conn, "rules", snapshot.get("rules", []), ("id",))
    n += _upsert(conn, "saved_searches", snapshot.get("saved_searches", []), ("id",))
    n += _upsert(conn, "entries", snapshot.get("entries", []), ("id",))
    n += _upsert(conn, "entry_state", snapshot.get("state", []), ("entry_id",))
    n += _upsert(conn, "entry_tags", snapshot.get("entry_tags", []), ("entry_id", "tag_id"))

    if "cursor" in snapshot:
        conn.execute("UPDATE node SET last_pull_seq = ? WHERE id = 1", (snapshot["cursor"],))
    if lamport := snapshot.get("server_lamport"):
        conn.execute("UPDATE node SET lamport = MAX(lamport, ?) WHERE id = 1", (lamport,))
    return n


def _upsert(
    conn: sqlite3.Connection, table: str, filas: list[dict], claves: tuple[str, ...]
) -> int:
    if not filas:
        return 0
    columnas = list(filas[0].keys())
    marcas = ",".join("?" * len(columnas))
    actualizables = [c for c in columnas if c not in claves]
    sets = ", ".join(f"{c} = excluded.{c}" for c in actualizables)
    sql = (
        f"INSERT INTO {table} ({','.join(columnas)}) VALUES ({marcas}) "
        f"ON CONFLICT({','.join(claves)}) DO UPDATE SET {sets}"
        if actualizables
        else f"INSERT OR IGNORE INTO {table} ({','.join(columnas)}) VALUES ({marcas})"
    )
    conn.executemany(sql, [tuple(f.get(c) for c in columnas) for f in filas])

    # El índice FTS no viaja en el snapshot: se reconstruye para las entradas nuevas.
    if table == "entries":
        for fila in filas:
            row = conn.execute(
                "SELECT rowid, title, author FROM entries WHERE id = ?", (fila["id"],)
            ).fetchone()
            if row:
                conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (row["rowid"],))
                conn.execute(
                    "INSERT INTO entries_fts (rowid, title, author, body) VALUES (?,?,?,?)",
                    (row["rowid"], row["title"], row["author"] or "", fila.get("summary") or ""),
                )
    return len(filas)
