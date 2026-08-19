"""Carpetas inteligentes: una consulta guardada, no un segundo motor de reglas.

Se resuelven con `repo.select_entries` y el índice FTS5 que ya existe. Duplicar
aquí la lógica del motor de reglas sería el error obvio: son cosas distintas —
las reglas actúan una vez, al llegar el artículo; una carpeta inteligente es una
vista que se recalcula cada vez que se mira.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .. import repo
from ..db import device_id, tick_lamport
from ..ids import new_id
from ..models import Entity, Entry, EntrySelection

__all__ = [
    "SavedSearch",
    "SavedSearchFilter",
    "delete_saved_search",
    "list_saved_searches",
    "run_saved_search",
    "save_saved_search",
    "saved_search_to_selection",
]


class SavedSearchFilter(BaseModel):
    """Filtros que acompañan a la consulta de texto."""

    model_config = ConfigDict(extra="ignore")

    feed_ids: list[str] = Field(default_factory=list)
    folder_ids: list[str] = Field(default_factory=list)
    tag_ids: list[str] = Field(default_factory=list)
    unread_only: bool = False
    starred_only: bool = False
    days: int | None = None
    limit: int = 200


class SavedSearch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str
    query: str = ""
    filter: SavedSearchFilter = Field(default_factory=SavedSearchFilter)
    position: int = 0


def saved_search_to_selection(
    conn: sqlite3.Connection, saved: SavedSearch | dict[str, Any]
) -> EntrySelection:
    """Traduce una carpeta inteligente a una selección de artículos."""
    search = _coerce(saved)
    f = search.filter
    since = None
    if f.days:
        from ..ids import now_ms

        since = now_ms() - f.days * 86_400_000
    return EntrySelection(
        feed_ids=list(f.feed_ids),
        folder_ids=list(f.folder_ids),
        tag_ids=list(f.tag_ids),
        query=search.query or None,
        unread_only=f.unread_only,
        starred_only=f.starred_only,
        since=since,
        limit=f.limit,
    )


def run_saved_search(
    conn: sqlite3.Connection, saved: SavedSearch | dict[str, Any], *, limit: int | None = None
) -> list[Entry]:
    sel = saved_search_to_selection(conn, saved)
    if limit is not None:
        sel.limit = limit
    return repo.select_entries(conn, sel)


# ------------------------------------------------------------------ persistencia
def list_saved_searches(conn: sqlite3.Connection) -> list[SavedSearch]:
    rows = conn.execute(
        "SELECT * FROM saved_searches WHERE deleted = 0 ORDER BY position, name"
    ).fetchall()
    salida = []
    for r in rows:
        try:
            filtro = SavedSearchFilter.model_validate(json.loads(r["filter_json"] or "{}"))
        except Exception:
            filtro = SavedSearchFilter()
        salida.append(
            SavedSearch(
                id=r["id"], name=r["name"], query=r["query"], filter=filtro, position=r["position"]
            )
        )
    return salida


def save_saved_search(
    conn: sqlite3.Connection, saved: SavedSearch | dict[str, Any], *, track: bool = True
) -> SavedSearch:
    search = _coerce(saved)
    lam, dev = tick_lamport(conn), device_id(conn)
    filtro = search.filter.model_dump_json()
    conn.execute(
        "INSERT INTO saved_searches (id, name, query, filter_json, position, deleted, lamport, "
        "device_id) VALUES (?,?,?,?,?,0,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
        "query=excluded.query, filter_json=excluded.filter_json, position=excluded.position, "
        "deleted=0, lamport=excluded.lamport, device_id=excluded.device_id",
        (search.id, search.name, search.query, filtro, search.position, lam, dev),
    )
    if track:
        repo.append_change(
            conn, Entity.SAVED_SEARCH, search.id, "query", search.query, lamport=lam, dev=dev
        )
        repo.append_change(
            conn, Entity.SAVED_SEARCH, search.id, "name", search.name, lamport=lam, dev=dev
        )
    return search


def delete_saved_search(conn: sqlite3.Connection, search_id: str, *, track: bool = True) -> None:
    lam, dev = tick_lamport(conn), device_id(conn)
    conn.execute(
        "UPDATE saved_searches SET deleted = 1, lamport = ?, device_id = ? WHERE id = ?",
        (lam, dev, search_id),
    )
    if track:
        repo.append_change(
            conn, Entity.SAVED_SEARCH, search_id, "deleted", True, lamport=lam, dev=dev
        )


def _coerce(saved: SavedSearch | dict[str, Any]) -> SavedSearch:
    if isinstance(saved, SavedSearch):
        return saved
    data = dict(saved)
    # La fila de la base trae `filter_json`; el API trae `filter`.
    if "filter_json" in data and "filter" not in data:
        try:
            data["filter"] = json.loads(data.pop("filter_json") or "{}")
        except Exception:
            data["filter"] = {}
    return SavedSearch.model_validate(data)
